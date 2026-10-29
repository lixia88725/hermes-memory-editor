'use strict';
// Vditor owns rendering only. The existing controller owns files, epochs and APIs.
async function createDocumentEditor(key,onChange,onComposition){
  const host=document.getElementById('ed-'+key);
  const rich=document.createElement('div');rich.id='rich-'+key;rich.className='rich';
  const source=document.createElement('textarea');source.id='source-'+key;source.className='source';source.hidden=true;source.spellcheck=false;source.setAttribute('aria-label',key.toUpperCase()+'.md Markdown source');
  host.append(rich,source);
  const mode=document.createElement('button');mode.id='mode-'+key;mode.className='icon-button';
  const modeIcons={"code-bracket": "<svg aria-hidden=\"true\" xmlns=\"http://www.w3.org/2000/svg\" fill=\"none\" viewBox=\"0 0 24 24\" stroke-width=\"1.5\" stroke=\"currentColor\" aria-hidden=\"true\" data-slot=\"icon\">\n  <path stroke-linecap=\"round\" stroke-linejoin=\"round\" d=\"M17.25 6.75 22.5 12l-5.25 5.25m-10.5 0L1.5 12l5.25-5.25m7.5-3-4.5 16.5\"/>\n</svg>\n", "pencil-square": "<svg aria-hidden=\"true\" xmlns=\"http://www.w3.org/2000/svg\" fill=\"none\" viewBox=\"0 0 24 24\" stroke-width=\"1.5\" stroke=\"currentColor\" aria-hidden=\"true\" data-slot=\"icon\">\n  <path stroke-linecap=\"round\" stroke-linejoin=\"round\" d=\"m16.862 4.487 1.687-1.688a1.875 1.875 0 1 1 2.652 2.652L10.582 16.07a4.5 4.5 0 0 1-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 0 1 1.13-1.897l8.932-8.931Zm0 0L19.5 7.125M18 14v4.75A2.25 2.25 0 0 1 15.75 21H5.25A2.25 2.25 0 0 1 3 18.75V8.25A2.25 2.25 0 0 1 5.25 6H10\"/>\n</svg>\n"};
  function updateModeIcon(isSource){mode.innerHTML=modeIcons[isSource?'pencil-square':'code-bracket'];mode.title=isSource?'切换到渲染编辑':'切换到 Markdown 源码';mode.setAttribute('aria-label',mode.title+' '+key.toUpperCase()+'.md');mode.setAttribute('aria-pressed',String(isSource));}
  updateModeIcon(false);
  document.querySelector('#panel-'+key+' .panel-actions').prepend(mode);
  let engine,raw='',loadedRaw='',canonical='',sourceMode=false,suppressed=true,ready=false,disabled=true,composing=false,settling=false,normalizing=false;
  const changed=()=>{onChange(key,raw,normalizing);};
  function flush(){
    if(!ready||suppressed||composing||settling)return raw;
    const value=sourceMode?source.value:engine.getValue();
    // Native textareas normalize line endings on assignment, not just on input.
    const next=sourceMode?raw:(value===canonical?loadedRaw:value);
    if(next!==raw){raw=next;normalizing=value!==canonical;changed();}
    return raw;
  }
  function load(text){
    suppressed=true;engine.setValue(text,true);loadedRaw=text;canonical=engine.getValue();raw=text;source.value=text;normalizing=false;suppressed=false;
  }
  function setDisabled(value){
    disabled=value;source.disabled=value;
    if(ready){if(value)engine.disabled();else engine.enable();}
    mode.disabled=value||composing||settling;
    rich.setAttribute('aria-disabled',String(value));
  }
  const root=document.getElementById('panel-'+key);
  root.addEventListener('compositionstart',()=>{if(disabled)return;composing=true;mode.disabled=true;onComposition();});
  root.addEventListener('compositionend',()=>{if(!composing)return;composing=false;settling=true;setTimeout(()=>{settling=false;flush();setDisabled(disabled);onComposition();},0);});
  source.addEventListener('input',()=>{if(suppressed)return;raw=source.value;normalizing=false;changed();});
  // Vditor's callback may be delayed; flush immediately after the native event too.
  rich.addEventListener('input',()=>queueMicrotask(flush));
  for(const type of ['drop','paste'])root.addEventListener(type,e=>{if(e.dataTransfer?.files?.length||e.clipboardData?.files?.length){e.preventDefault();e.stopImmediatePropagation();}},true);
  mode.onclick=()=>{
    if(disabled||composing||settling||busy())return;
    flush();
    if(!sourceMode){source.value=raw;sourceMode=true;source.hidden=false;rich.hidden=true;source.focus();}
    else{load(raw);sourceMode=false;source.hidden=true;rich.hidden=false;engine.focus();}
    updateModeIcon(sourceMode);
  };
  await new Promise(resolve=>{
    engine=new Vditor(rich.id,{
      cdn:new URL('vendor/vditor',location.href).href.replace(/\/$/,''),lang:'zh_CN',mode:'ir',value:'',
      cache:{enable:false},toolbar:[],toolbarConfig:{hide:true,pin:false},height:'100%',minHeight:100,
      theme:document.documentElement.dataset.theme==='dark'?'dark':'classic',undoDelay:150,
      outline:{enable:false},counter:{enable:false},resize:{enable:false},hint:{emoji:{},extend:[],parse:false},
      link:{isOpen:false},image:{isPreview:false},upload:{url:'',linkToImgUrl:'',handler:()=>null},
      preview:{actions:[],hljs:{enable:false},markdown:{autoSpace:false,fixTermTypo:false,paragraphBeginningSpace:false,gfmAutoLink:false,codeBlockPreview:false,mathBlockPreview:false,footnotes:false,toc:false,mark:false,sanitize:true,listStyle:false},render:{media:{enable:false}}},
      input:flush,after:()=>{ready=true;suppressed=false;canonical=engine.getValue();const editable=rich.querySelector('.vditor-ir [contenteditable]');editable.setAttribute('role','textbox');editable.setAttribute('aria-multiline','true');editable.setAttribute('aria-label',key.toUpperCase()+'.md IR editor');setDisabled(true);resolve();}
    });
  });
  window.addEventListener('editor-theme-change',e=>engine.setTheme(e.detail==='dark'?'dark':'classic'));
  return {getValue:flush,setValue:load,setDisabled,isComposing:()=>composing||settling,focus:()=>sourceMode?source.focus():engine.focus()};
}
