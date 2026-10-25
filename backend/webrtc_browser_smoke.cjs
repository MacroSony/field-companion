/* Opt-in real browser/API test. Run only against webrtc_smoke_server.py. */
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.FIELD_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..'), output = path.join(root, 'out', 'audio-lab');
const report = {passed: false, fixtures: ['synthetic microphone', 'taxonomy', 'BirdNET disabled', 'research runner'],
  physical_phone: false, acoustic_echo_test: false, checks: [], browser_errors: []};
(async () => {
  const browser = await chromium.launch({channel: 'chrome', headless: true, args: [
    '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
    '--use-file-for-fake-audio-capture=' + path.join(output, 'browser-mic.wav'), '--autoplay-policy=no-user-gesture-required']});
  try {
    const context = await browser.newContext({viewport: {width: 420, height: 900}, acceptDownloads: true});
    await context.grantPermissions(['microphone']);
    const page = await context.newPage();
    page.on('pageerror', error => report.browser_errors.push(error.name + ': ' + error.message.slice(0, 180)));
    const sid = 'rtc-smoke-' + Date.now();
    await page.goto('http://127.0.0.1:8803/?transport=webrtc&session=' + sid);
    await page.evaluate(() => {
      const Original = window.FieldWebRTCAudio;
      window.testMediaEvents = [];
      window.FieldWebRTCAudio = class extends Original {
        constructor(options) {
          const onEvent = options.onEvent;
          super({...options, onEvent: (type, fields) => { window.testMediaEvents.push({type, fields, at: performance.now()}); onEvent(type, fields); }});
          window.testRTC = this;
        }
      };
    });
    await page.locator('#start').click();
    await page.locator('#settings-open').click();
    await page.getByText('Audio experiments and diagnostics', {exact: true}).click();
    await page.locator('#diagnostics-record').click();
    await page.waitForFunction(() => document.querySelector('#diagnostics-status').textContent.includes('Recording locally'));
    await page.waitForFunction(() => document.querySelector('#model').textContent.includes('gpt-realtime'), null, {timeout: 45000});
    await page.waitForFunction(() => document.querySelector('#user-caption').textContent.toLowerCase().includes('hear me'), null, {timeout: 45000});
    await page.waitForFunction(() => !document.querySelector('#assistant-caption').textContent.includes('Ready for your next'), null, {timeout: 30000});
    report.checks.push({case: 'real WebRTC microphone, transcription and server-controlled voice reply', passed: true,
      user: await page.locator('#user-caption').innerText(), assistant: await page.locator('#assistant-caption').innerText()});
    await page.waitForFunction(() => window.testRTC?.playing);
    const bargeAudio = fs.readFileSync(path.join(output, 'barge.wav')).toString('base64');
    await page.evaluate(async encoded => {
      const rtc = window.testRTC, bytes = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
      const buffer = await rtc.context.decodeAudioData(bytes.buffer);
      const destination = rtc.context.createMediaStreamDestination(), source = rtc.context.createBufferSource();
      source.buffer = buffer; source.connect(destination);
      const sender = rtc.peer.getSenders().find(s => s.track?.kind === 'audio');
      await sender.replaceTrack(destination.stream.getAudioTracks()[0]);
      window.testBarge = {source, destination, sender, started: performance.now()};
      source.onended = () => sender.replaceTrack(rtc.stream.getAudioTracks()[0]);
      source.start();
    }, bargeAudio);
    await page.waitForFunction(() => window.testMediaEvents.some(e => e.type === 'output_audio_buffer.cleared' && e.at > window.testBarge.started), null, {timeout: 12000});
    await page.waitForFunction(() => /checklist/i.test(document.querySelector('#user-caption').textContent), null, {timeout: 15000});
    report.checks.push({case: 'natural spoken question interrupts and clears current WebRTC playback', passed: true});
    await page.locator('#text-input').fill('Two mallards. Add them to my checklist.');
    await page.locator('#send').click();
    await page.waitForFunction(() => document.querySelector('#checklist').textContent.includes('Mallard'), null, {timeout: 30000});
    report.checks.push({case: 'existing checklist tools controlled from sideband', passed: true});
    await page.locator('#text-input').fill('Could it be a Cooper\'s Hawk instead?');
    await page.locator('#send').click();
    await page.waitForFunction(() => document.querySelector('#investigations').textContent.includes('R1'), null, {timeout: 30000});
    report.checks.push({case: 'background investigation scheduled over the existing app path', passed: true});
    await page.locator('#text-input').fill('How is the first investigation going?');
    await page.locator('#send').click();
    await page.waitForFunction(() => /R1/i.test(document.querySelector('#assistant-caption').textContent)
      && /retriev|source|complet|ready|running|queue/i.test(document.querySelector('#assistant-caption').textContent)
      && document.querySelector('#assistant-caption').classList.contains('partial') === false, null, {timeout: 20000});
    await page.waitForFunction(() => /ready|complete/i.test(document.querySelector('#investigations').textContent), null, {timeout: 15000});
    report.checks.push({case: 'immediate follow-up survives cancellation and investigation still completes', passed: true});
    await page.locator('#diagnostics-record').click();
    const download = page.waitForEvent('download');
    await page.locator('#diagnostics-save').click();
    const file = await download;
    await file.saveAs(path.join(output, 'browser-diagnostic.html'));
    const html = fs.readFileSync(path.join(output, 'browser-diagnostic.html'), 'utf8');
    if (!html.includes('data:audio/wav;base64,') || !html.includes('input_vad')) throw new Error('Diagnostic is missing audio/timeline evidence');
    const pcm = Buffer.from(html.match(/data:audio\/wav;base64,([A-Za-z0-9+/=]+)/)[1], 'base64');
    let micEnergy = 0, playbackEnergy = 0;
    for (let i = 44; i + 3 < pcm.length; i += 4) {
      micEnergy += pcm.readInt16LE(i) ** 2; playbackEnergy += pcm.readInt16LE(i + 2) ** 2;
    }
    if (!micEnergy || !playbackEnergy) throw new Error('Both diagnostic audio channels must contain signal');
    if (/sk-proj-|X-Field-RTC|a=fingerprint:/.test(html)) throw new Error('Private signaling data leaked into diagnostics');
    const timeline = JSON.parse(html.split('id="data">')[1].split('</script>')[0]);
    if (timeline.events.filter(e => e.event === 'input_vad_start').length < 2) throw new Error('Missing microphone/VAD evidence for the spoken interruption');
    if (!timeline.events.some(e => e.event === 'tool' && e.fields.fields?.name === 'get_task_status')) throw new Error('Progress question did not use the existing status tool');
    report.checks.push({case: 'both captured microphone and playback reference have nonzero audio', passed: true});
    report.checks.push({case: 'local stereo recording and event replay export', passed: true});
    report.capture = await page.locator('#capture-settings').innerText();
    report.environment = await page.locator('#environment-status').innerText();
    await page.locator('#settings-dialog .dialog-close').click();
    await page.screenshot({path: path.join(output, 'browser-smoke.png'), fullPage: true});
    await page.locator('#stop').click();
    report.passed = report.browser_errors.length === 0;
  } finally { await browser.close(); }
})().catch(error => {
  report.error_type = error.name; report.failure = error.message.slice(0, 500);
}).finally(() => {
  fs.writeFileSync(path.join(output, 'browser-smoke.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report)); process.exitCode = report.passed ? 0 : 1;
});
