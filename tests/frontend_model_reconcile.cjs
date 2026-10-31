// 执行真实前端迁移函数，覆盖旧默认和历史会话。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '..', 'static', 'index.html'), 'utf8');
function fn(name){ const start=html.indexOf('function '+name+'('); return html.slice(start, html.indexOf('\n}',start)+2); }
const data = new Map([['defaultModel','claude-old'],['sessionModel:active','claude-old'],['sessionModel:history','claude-old']]);
const context=vm.createContext({
  MODELS:[['proxy/current','Current',true]], currentModel:'claude-old', currentSessionId:'active',
  FALLBACK_MODEL_ID:'claude-sonnet-5', _modelsLoaded:true, _prefsStamped:false,
  EFFORTS:[], currentEffort:'', _defEffort:()=>'', syncModelBtn(){},
  localStorage:{getItem:k=>data.has(k)?data.get(k):null,setItem:(k,v)=>data.set(k,v)},
  _mKey:sid=>'sessionModel:'+sid, _eKey:sid=>'sessionEffort:'+sid,
  savePref:(k,v)=>data.set(k,v), _defModel:()=>data.get('defaultModel')
});
const line=html.split('\n').find(x=>x.startsWith('const _availableModelFallback ='));
assert(line);
vm.runInContext(line+'\n'+fn('_reconcileModels')+'\n'+fn('adoptSessionPrefs'),context);
vm.runInContext('_reconcileModels()',context);
assert.equal(data.get('defaultModel'),'proxy/current');
assert.equal(data.get('sessionModel:active'),'proxy/current');
assert.equal(context.currentModel,'proxy/current');
vm.runInContext('adoptSessionPrefs("history")',context);
assert.equal(data.get('sessionModel:history'),'proxy/current');
assert.equal(context.currentModel,'proxy/current');
