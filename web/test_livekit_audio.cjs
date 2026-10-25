/* Offline phone transport tests: real app adapter, fake room/network. */
const test = require('node:test');
const assert = require('node:assert/strict');
const {FieldLiveKitAudio} = require('./livekit-audio.js');

function makeAudio() {
  const connections = [];
  const node = label => ({label, gain: {value: 1}, connect(target) {connections.push([label,target.label]); return target;}, disconnect() {}});
  const output=node('speaker'), track={enabled: true, getSettings:()=>({echoCancellation:true})};
  const stream={getAudioTracks:()=>[track]};
  const context={createGain:()=>node('diagnostic'),createMediaStreamSource:()=>node('reference')};
  return {audio:new FieldLiveKitAudio({context,stream,output,endpoint:'/api/livekit/connect',onFailure() {}}),track,connections};
}

test('mute changes the native remote audio element and its diagnostic tap together', () => {
  const {audio}=makeAudio(),element={muted:false},gain={gain:{value:1}};
  audio.elements.add(element);audio.references.add({gain});
  audio.setMuted(true);assert.equal(element.muted,true);assert.equal(gain.gain.value,0);
  audio.setMuted(false);assert.equal(element.muted,false);assert.equal(gain.gain.value,1);
});

test('ambient capture is separate and a native voice track is never enabled before ready', () => {
  const {audio,track}=makeAudio();
  audio.updateMic();assert.equal(track.enabled,false);
  audio.ready=true;audio.updateMic();assert.equal(track.enabled,true);
  audio.setMic(false);assert.equal(track.enabled,false);
});

test('phone publishes exactly one voice track and attaches remote speech natively', async () => {
  const originals={fetch:global.fetch, LivekitClient:global.LivekitClient,document:global.document,MediaStream:global.MediaStream};
  const {audio,track,connections}=makeAudio();let room;
  const element={play:async()=>{},pause(){},remove(){},dataset:{},muted:false};
  const remote={kind:'audio',mediaStreamTrack:{},attach:()=>element};
  global.fetch=async()=>({ok:true,json:async()=>({url:'wss://example.livekit.cloud',token:'short-lived-room-token',room:'test'})});
  global.document={body:{append(){}}};global.MediaStream=class {};
  global.LivekitClient={Track:{Kind:{Audio:'audio'},Source:{Microphone:'microphone'}},
    RoomEvent:{TrackSubscribed:'track',TrackUnsubscribed:'untrack',Disconnected:'disconnected',Reconnecting:'reconnecting'},
    Room:class {constructor(){room=this;this.events={};this.published=[];this.localParticipant={publishTrack:async(t,options)=>this.published.push([t,options])};}
      on(name,handler){this.events[name]=handler;return this;}async connect(){this.events.track(remote,{}, {identity:'agent-test'});}disconnect(){}}};
  audio.openEnvironment=async()=>{};
  try {
    await audio.connect('private-capability');
    assert.equal(room.published.length,1);assert.equal(room.published[0][0],track);
    assert.equal(audio.elements.size,1);
    assert.ok(!connections.some(([source,target])=>source==='reference' && target==='speaker'));
    room.events.track(remote,{trackSid:'voice'}, {identity:'agent-test'});
    room.events.track(remote,{trackSid:'voice'}, {identity:'agent-test'});
    assert.equal(audio.elements.size,1); assert.equal(audio.references.size,1);
    // An unrelated participant and a stale unsubscribe cannot remove the voice.
    room.events.track({kind:'audio'}, {}, {identity:'other-phone'});
    room.events.untrack({kind:'audio'},{trackSid:'voice'});
    assert.equal(audio.elements.size,1);
    room.events.untrack(remote,{trackSid:'voice'});
    assert.equal(audio.elements.size,0);assert.equal(audio.references.size,0);
    audio.close();assert.equal(audio.closed,true);
    assert.equal(track.enabled,false);
  } finally {Object.assign(global,originals);}
});
