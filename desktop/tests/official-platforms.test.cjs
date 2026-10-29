'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const { execFileSync }=require('node:child_process');
const { platforms }=require('../lib/platform-options.cjs');
const { launchScript }=require('../lib/deploy.cjs');
const { LocalDeployment }=require('../lib/local.cjs');

test('platform contract keeps legacy default and rejects coercion or all-disabled',()=>{
 assert.deepEqual(platforms(),{onebot:true,qq_official:false});
 for(const value of [null,{},[],{onebot:false,qq_official:false},{onebot:0,qq_official:true},{onebot:false,qq_official:true,url:'https://x'}])assert.throws(()=>platforms(value));
 assert.deepEqual(platforms({onebot:false,qq_official:true}),{onebot:false,qq_official:true});
});
test('server privileged install pins exact platform choice, no credentials or host paths from renderer',()=>{
 const manifest={files:Object.fromEntries(['bootstrap.sh','installer.py','environment.nix','gaoji-runtime.tar.gz'].map(name=>[name,{sha256:'a'.repeat(64)}]))};
 const script=launchScript('b'.repeat(32),'/tmp/gaoji-upload.1234567890',manifest,0,undefined,undefined,undefined,{onebot:false,qq_official:true});
 execFileSync('/bin/sh',['-n'],{input:script});
 assert.match(script,/platforms.json/);assert.match(script,/"onebot":false,"qq_official":true/);
 assert.match(script,/test "\$\(cat "\$jobroot\/platforms.json"\)" =/);
 assert.doesNotMatch(script,/AppSecret|app_secret/);
 assert.throws(()=>launchScript('b'.repeat(32),'/tmp/gaoji-upload.1234567890',manifest,0,undefined,undefined,undefined,{onebot:false,qq_official:true,path:'/etc'}));
});
test('local record preserves explicit platform and old record compatibility',t=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'qqo-m8-local-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
 const local=new LocalDeployment(root,root,{});
 const old={schema:1,job:'a'.repeat(32),checksum:'b'.repeat(64),port:54321,botPort:54322,engineId:'TEST',credential:'encrypted'};
 local.write(old);assert.equal(local.read().platforms,undefined);
 local.write({...old,platforms:{onebot:false,qq_official:true}});assert.deepEqual(local.read().platforms,{onebot:false,qq_official:true});
 local.write({...old,platforms:{onebot:false,qq_official:false}});assert.throws(()=>local.read());
});
