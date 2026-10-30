import {beforeEach,describe,expect,it,vi} from 'vitest';
import {defaults,readSettings,writeSettings} from './settings';
const values=new Map();
beforeEach(()=>{values.clear();vi.stubGlobal('localStorage',{getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,value)})});
describe('Sona settings',()=>{
  it('loads safe defaults and persists separate preferences per user',()=>{expect(readSettings('alice').crossfade).toBe(0);writeSettings('alice',{...defaults,crossfade:2.5,downloadFormat:'mp3'});expect(readSettings('alice').crossfade).toBe(2.5);expect(readSettings('bob').crossfade).toBe(0)});
  it('recovers from malformed stored preferences',()=>{values.set('sona-settings:alice','{');expect(readSettings('alice')).toEqual(defaults)});
});
