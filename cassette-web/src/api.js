export async function call(path, options={}) {
  const response=await fetch(path,{credentials:'same-origin',...options});
  const data=await response.json().catch(()=>({}));
  if(!response.ok) throw new Error(typeof data.detail==='string'?data.detail:`Request failed (${response.status})`);
  return data;
}
export const sub=(endpoint,params={})=>call('/api/sub/'+endpoint+'?'+new URLSearchParams(params),['star','unstar','scrobble','createPlaylist','updatePlaylist','deletePlaylist','savePlayQueue'].includes(endpoint)?{method:'POST'}:{});
export const bridge=(path,params={})=>call('/api/bridge/'+path+'?'+new URLSearchParams(params));
export const post=(path,data)=>call(path,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(data)});
export const cover=(id,size=400)=>id?'/api/media/getCoverArt?'+new URLSearchParams({id,size}):null;
export const stream=(id,format)=>'/api/media/stream?'+new URLSearchParams({id,maxBitRate:'320',...(format?{format}:{})});
export const list=value=>!value?[]:Array.isArray(value)?value:[value];
