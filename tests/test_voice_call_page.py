import re
import subprocess
import unittest
from pathlib import Path


PAGE = Path(__file__).resolve().parents[1] / 'src/plugins/ai_chat/voice_call.html'
BROWSER = r'''
import assert from 'node:assert/strict';
const elements = {status: {}, start: {}, end: {}};
const document = {getElementById: id => elements[id]};
const location = {protocol: 'https:', host: 'TEST_HOST', pathname: '/voice-call', hash: '#TEST_TOKEN'};
const history = {replaceState: (_, __, path) => { assert.equal(path, location.pathname); location.hash = ''; }};
const events = {}, window = {addEventListener: (name, fn) => events[name] = fn};
const played = [], sent = [], track = {stop() { this.stopped = true; }};
const media = {getTracks: () => [track], getAudioTracks: () => [track]};
let grant, requested = false, resumed = false, socket, audio, processorNode, blob;
const mediaDevices = {getUserMedia: ({audio}) => {
  assert.ok(audio.echoCancellation && audio.noiseSuppression); requested = true;
  return new Promise(r => grant = r);
}};
const navigator = {mediaDevices};
const URL = {createObjectURL(b) { blob = b; return 'blob:test'; }, revokeObjectURL() { blob = null; }};
const sampleRate = 44100;
class AudioWorkletProcessor {}
function registerProcessor(name, cls) { processorNode = new cls(); }
class AudioWorkletNode {
  constructor() {
    this.port = {close() {}, postMessage: bytes => this.port.onmessage({data: bytes})};
    processorNode.port = this.port;
  }
  connect() {} disconnect() {}
}
class AudioContext {
  sampleRate = sampleRate; currentTime = 1;
  audioWorklet = {addModule: async () => eval(await blob.text())};
  constructor() { audio = this; }
  resume() { resumed = true; return Promise.resolve(); }
  close() { this.state = 'closed'; }
  createMediaStreamSource() { return {connect() {}, disconnect() {}}; }
  createBuffer(n, size, rate) {
    const samples = new Float32Array(size);
    return {samples, rate, duration: size / rate, getChannelData: () => samples};
  }
  createBufferSource() {
    return {connect() {}, disconnect() {}, start(at) { this.at = at; played.push(this); }, stop() { this.stopped = true; }};
  }
}
class WebSocket {
  static CLOSING = 2;
  constructor(url) { assert.equal(url, 'wss://TEST_HOST/voice-call/live'); socket = this; this.readyState = 0; }
  send(value) { sent.push(typeof value === 'string' ? JSON.parse(value) : value); }
  close(code, reason) { this.closeCode = code; this.readyState = 2; }
}
async function start() {
  assert.equal(location.hash, '');
  const started = elements.start.onclick();
  assert.ok(resumed && requested); // Both happen before the gesture handler yields.
  assert.equal(played.length, 1); // Silent unlock, not the acknowledgement.
  grant(media); await started;
  assert.equal(blob, null); socket.readyState = 1; socket.onopen();
  assert.deepEqual(sent, [{token: 'TEST_TOKEN'}]);
}
function mic(value, ms) {
  for (let n = Math.round(sampleRate * ms / 1000); n > 0; n -= 128)
    processorNode.process([[new Float32Array(Math.min(n, 128)).fill(value)]]);
}
function receive(data) { socket.onmessage({data: data instanceof ArrayBuffer ? data : JSON.stringify(data)}); }
'''


class VoiceCallPageTest(unittest.TestCase):
    def run_page(self, scenario):
        script = re.search(r'<script type="module">(.*?)</script>', PAGE.read_text(), re.S)[1]
        program = BROWSER + '\n(() => {' + script + '})();\n' + scenario
        result = subprocess.run(['node', '--input-type=module'], input=program,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_turns_and_barge_in(self):
        self.run_page('''
          await start();
          const pcm = new Uint8Array([0, 128, 0, 64]).buffer;
          receive(pcm); assert.equal(played.length, 1);
          mic(.5, 140); mic(0, 10); // False start must not be sent.
          mic(-.5, 150); mic(.5, 100); mic(0, 490);
          assert.equal(sent.length, 1); assert.equal(played.length, 1);
          mic(0, 10);
          assert.equal(sent[1].byteLength, 16000 * .25 * 2);
          const voice = new DataView(sent[1]);
          assert.equal(voice.getInt16(0, true), -16384);
          assert.equal(voice.getInt16(voice.byteLength - 2, true), 16384);
          assert.equal(played.length, 2);
          assert.deepEqual([...played[1].buffer.samples], [-1, .5]);
          assert.equal(played[1].buffer.rate, 24000);
          played[1].onended(); // Waiting for turn 1; nothing currently playing.
          mic(.5, 140); assert.equal(sent.length, 2);
          mic(.5, 10); assert.deepEqual(sent[2], {type: 'interrupt'});
          receive({type: 'reply', turn: 1}); receive(pcm);
          mic(0, 500); assert.equal(played.length, 3);
          receive(pcm); receive({type: 'done', turn: 1});
          assert.equal(played.length, 3); // No stale reply after the second utterance.
          receive({type: 'reply', turn: 2}); receive(pcm); receive(pcm);
          receive({type: 'done', turn: 2});
          for (const i of [3, 4]) assert.equal(played[i].at, played[i-1].at + played[i-1].buffer.duration);
          mic(.5, 150);
          assert.deepEqual(sent[4], {type: 'interrupt'});
          assert.ok(played.slice(2).every(s => s.stopped)); // done, but audio was still queued.
          elements.end.onclick(); assert.equal(socket.closeCode, 1000);
          assert.ok(track.stopped); assert.equal(audio.state, 'closed');
        ''')

    def test_server_error_survives_close(self):
        self.run_page('''
          await start(); receive({type: 'error', message: '链接已过期'});
          socket.onclose({code: 1008, reason: ''});
          assert.equal(elements.status.textContent, '已结束：链接已过期');
          assert.ok(track.stopped); assert.equal(audio.state, 'closed');
        ''')

    def test_close_reason(self):
        self.run_page('''
          await start(); socket.readyState = 3; socket.onclose({code: 1000, reason: '通话时间已到'});
          assert.equal(elements.status.textContent, '已结束：通话时间已到');
          assert.ok(track.stopped); assert.equal(audio.state, 'closed');
        ''')

    def test_pagehide_before_permission(self):
        self.run_page('''
          const started = elements.start.onclick(); events.pagehide(); grant(media); await started;
          assert.ok(track.stopped); assert.equal(audio.state, 'closed'); assert.equal(socket, undefined);
        ''')
