"""Per-profile DroppedNeedle companion sessions. No raw token is exposed by API.

The Music Profiles Bridge profile bearer identifies the caller. DroppedNeedle
credentials are presented once to exchange for a revocable 30-day companion
session. Only that encrypted companion token is persisted.
"""
import hmac
import logging
import sqlite3
import time
from typing import Any

import httpx
from fastapi import HTTPException

LOG = logging.getLogger('music_profiles.dn_accounts')
DEVICE_NAME = 'Music Profiles Bridge'
DEVICE_USER_AGENT = f'DroppedNeedle companion · {DEVICE_NAME}'


def initialize(connection):
    with connection() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS droppedneedle_accounts (
            profile_id TEXT PRIMARY KEY,
            dn_user_id TEXT NOT NULL UNIQUE,
            dn_username TEXT NOT NULL,
            encrypted_token BLOB NOT NULL,
            linked_at INTEGER NOT NULL
        )''')


def linked_metadata(connection, profile_id: str) -> dict[str, Any] | None:
    with connection() as db:
        record = db.execute('''SELECT dn_user_id, dn_username, linked_at
            FROM droppedneedle_accounts WHERE profile_id=?''', (profile_id,)).fetchone()
    return dict(record) if record else None


def linked_token(connection, cipher, profile_id: str) -> str | None:
    with connection() as db:
        record = db.execute('SELECT encrypted_token FROM droppedneedle_accounts WHERE profile_id=?',
                            (profile_id,)).fetchone()
    if not record:
        return None
    try:
        return cipher.decrypt(record['encrypted_token']).decode('utf-8')
    except Exception:
        LOG.error('Invalid encrypted DroppedNeedle credential for a profile')
        raise HTTPException(503, 'Stored DroppedNeedle connection could not be decrypted; contact administrator')


def save_link(connection, cipher, profile_id: str, dn_user_id: str,
              dn_username: str, token: str):
    if not dn_user_id or not token:
        raise HTTPException(502, 'DroppedNeedle did not provide a valid account identity')
    try:
        with connection() as db:
            existing = db.execute('SELECT profile_id FROM droppedneedle_accounts WHERE dn_user_id=?',
                                  (dn_user_id,)).fetchone()
            if existing and not hmac.compare_digest(existing['profile_id'], profile_id):
                raise HTTPException(409, 'That DroppedNeedle account is already linked to another family profile')
            db.execute('''INSERT INTO droppedneedle_accounts
                (profile_id,dn_user_id,dn_username,encrypted_token,linked_at)
                VALUES (?,?,?,?,?) ON CONFLICT(profile_id) DO UPDATE SET
                  dn_user_id=excluded.dn_user_id,
                  dn_username=excluded.dn_username,
                  encrypted_token=excluded.encrypted_token,
                  linked_at=excluded.linked_at''',
                (profile_id,dn_user_id,dn_username,cipher.encrypt(token.encode()),int(time.time())))
    except sqlite3.IntegrityError:
        raise HTTPException(409, 'That DroppedNeedle account is already linked to another family profile')


def unlink(connection, profile_id: str):
    with connection() as db:
        db.execute('DELETE FROM droppedneedle_accounts WHERE profile_id=?', (profile_id,))


async def authenticate(client: httpx.AsyncClient, base_url: str, username: str, password: str) -> str:
    try:
        response = await client.post(base_url + '/api/v1/auth/login',
                                     json={'username':username, 'password':password}, timeout=15)
    except httpx.HTTPError:
        raise HTTPException(502, 'DroppedNeedle could not be reached')
    if response.status_code in (400, 401, 403):
        raise HTTPException(401, 'Invalid DroppedNeedle username or password')
    if response.status_code == 429:
        raise HTTPException(429, 'Too many DroppedNeedle login attempts; try again later')
    if response.status_code >= 400:
        raise HTTPException(502, 'DroppedNeedle login is temporarily unavailable')
    try:
        token = response.json()['token']
        if not isinstance(token, str) or not token:
            raise ValueError('empty')
    except (ValueError, KeyError, TypeError):
        raise HTTPException(502, 'Unexpected DroppedNeedle login response')
    return token


async def identity(client: httpx.AsyncClient, base_url: str, token: str) -> dict[str, str]:
    try:
        response = await client.get(base_url + '/api/v1/auth/me',
                                    headers={'Authorization': 'Bearer ' + token}, timeout=12)
    except httpx.HTTPError:
        raise HTTPException(502, 'DroppedNeedle is unavailable')
    if response.status_code in (401, 403):
        raise HTTPException(401, 'DroppedNeedle session expired or was revoked')
    if response.status_code != 200:
        raise HTTPException(502, 'Could not verify DroppedNeedle account')
    try:
        payload = response.json()
        # The /auth/me endpoint returns the user directly.
        if isinstance(payload, dict) and 'user' in payload and isinstance(payload['user'], dict):
            payload = payload['user']
        account_id = payload['id']
        display = payload.get('username') or payload.get('username_display') or payload.get('display_name') or account_id
        if not isinstance(account_id, str) or not account_id or not isinstance(display, str):
            raise ValueError('missing identity')
    except (ValueError, TypeError, KeyError):
        raise HTTPException(502, 'Unexpected DroppedNeedle identity response')
    return {'dn_user_id':account_id, 'dn_username':display}


async def companion_session(client: httpx.AsyncClient, base_url: str, standard_token: str):
    """Standard token only; DN rejects companion -> companion minting by design."""
    try:
        response = await client.post(base_url + '/api/v1/auth/device-sessions',
                                     headers={'Authorization': 'Bearer ' + standard_token},
                                     json={'device_name':DEVICE_NAME}, timeout=12)
    except httpx.HTTPError:
        raise HTTPException(502, 'DroppedNeedle is unavailable')
    if response.status_code in (401, 403):
        raise HTTPException(401, 'A standard DroppedNeedle login session is required to link your account')
    if response.status_code == 404:
        raise HTTPException(502, 'This DroppedNeedle version does not support companion sessions; update DroppedNeedle')
    if response.status_code != 200:
        raise HTTPException(502, 'DroppedNeedle could not issue a companion session')
    try:
        token = response.json()['token']
        if not isinstance(token, str) or not token:
            raise ValueError('invalid token')
    except (ValueError, KeyError, TypeError):
        raise HTTPException(502, 'Unexpected companion session response')
    return token


async def logout_temporary_session(client: httpx.AsyncClient, base_url: str, standard_token: str):
    """Clean up the standard session minted by login; never revoke pasted sessions."""
    try:
        response=await client.post(base_url+'/api/v1/auth/logout',
                    headers={'Authorization':'Bearer '+standard_token},timeout=8)
        response.raise_for_status()
    except httpx.HTTPError:
        LOG.warning('Temporary DroppedNeedle login session revocation not confirmed')


async def revoke_companion(client: httpx.AsyncClient, base_url: str, token: str):
    """Best-effort remote revocation of this exact Bridge companion session."""
    if not token:
        return
    try:
        headers = {'Authorization': 'Bearer ' + token}
        r = await client.get(base_url + '/api/v1/auth/sessions', headers=headers, timeout=8)
        r.raise_for_status()
        sessions = r.json().get('sessions', [])
        for sess in sessions:
            if (sess.get('session_kind') == 'companion' and
                    sess.get('user_agent') == DEVICE_USER_AGENT and sess.get('id')):
                response=await client.delete(base_url + '/api/v1/auth/sessions/' + sess['id'],
                                             headers=headers, timeout=8)
                response.raise_for_status()
                break
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        # User can revoke stale sessions manually in DroppedNeedle Settings.
        LOG.warning('Remote companion revocation not confirmed; local token discarded')
