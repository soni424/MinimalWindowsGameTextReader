using System.Diagnostics;
using System.Drawing;
using System.IO;
using GameTextReader.Core;
using GameTextReader.Windows;
using NUnit.Framework;

namespace GameTextReader.Tests;

[NonParallelizable]
public class NativeTests
{
    [Test]
    public void ActualDesktopCaptureSupportsCombinedRasterOperation()
    {
        var monitor = NativeDesktop.Monitors()[0]; using var image = NativeDesktop.Capture([monitor.Left + 20, monitor.Top + 20, monitor.Left + 120, monitor.Top + 80]);
        Assert.That(image.Width, Is.EqualTo(100)); Assert.That(image.Height, Is.EqualTo(60)); Assert.That(image.GetPixel(0, 0).A, Is.EqualTo(255));
    }
    [Test]
    public async Task SavedSapiIdsRemainCompatibleWithPythonTokenIdentifiers()
    {
        var voices = await SpeechService.ListVoicesAsync(); var sapi = voices.Where(v => v.Backend == "sapi").ToArray(); Assert.That(sapi, Is.Not.Empty);
        Assert.That(sapi.All(v => v.Id.StartsWith("sapi:HKEY_", StringComparison.OrdinalIgnoreCase)), Is.True, "Saving a .NET-only short token would prevent Python rollback from selecting the voice: " + string.Join("; ", sapi.Select(v => v.Id)));
    }
    [Test]
    public async Task ActualWindowsOcrReadsStandardAndEnhanced()
    {
        using var image = new Bitmap(1000, 220); using (var g = Graphics.FromImage(image)) { g.Clear(Color.Black); using var font = new Font("Segoe UI", 30); g.DrawString("Sentinel 27 Testimony", font, Brushes.White, 20, 20); g.DrawString("I definitely saw an Angel!", font, Brushes.White, 20, 95); }
        var ocr = new OcrService(); foreach (var mode in new[] { "standard", "enhanced" }) { var r = await ocr.RecognizeAsync(image, mode, CancellationToken.None); Assert.That(r.Text, Does.Contain("Testimony").And.Contain("Angel")); TestContext.Out.WriteLine($"{mode} OCR: {r.ElapsedMs:0} ms, {r.Variant}, {r.Attempts} passes"); }
    }
    [TestCase("winrt")]
    [TestCase("sapi")]
    public async Task NativeMutedPlaybackSupportsProgressPauseSpeedAndRapidSeeking(string backend)
    {
        var voice = (await SpeechService.ListVoicesAsync()).FirstOrDefault(v => v.Backend == backend); Assert.That(voice, Is.Not.Null);
        using var speech = new SpeechService(); var source = "Heading\n\nFirst sentence contains repeated words repeated words. Second sentence explains empirical evidence. Third sentence ends the passage."; var document = SpeechText.Prepare(source);
        var started = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously); var progress = new TaskCompletionSource<SpeechProgress>(TaskCreationOptions.RunContinuationsAsynchronously); var errors = new List<string>();
        speech.Started += (_, _) => started.TrySetResult(); speech.Progress += p => progress.TrySetResult(p); speech.Error += error => { lock (errors) errors.Add(error); started.TrySetException(new Exception(error)); };
        var clock = Stopwatch.StartNew(); var id = speech.Submit(document, new(voice!.Id, 0, 0, PlaybackRate: .9, Reader: true)); await started.Task.WaitAsync(TimeSpan.FromSeconds(30)); TestContext.Out.WriteLine($"{backend} warm first audio/ready: {clock.Elapsed.TotalMilliseconds:0} ms");
        var p = await progress.Task.WaitAsync(TimeSpan.FromSeconds(10)); Assert.That(p.SourceText, Is.EqualTo(source)); var state = (await speech.DiagnosticsAsync()).Single(r => r.Id == id); Assert.That(state.Rate, Is.EqualTo(.9).Within(.02)); Assert.That(state.HasTiming, Is.True);
        speech.Pause(id, source, true); await Task.Delay(150); Assert.That((await speech.DiagnosticsAsync()).Single(r => r.Id == id).Paused, Is.True);
        speech.SetRate(id, source, 1.3); speech.Seek(id, source, source.IndexOf("Second", StringComparison.Ordinal), false, true); await Task.Delay(150); state = (await speech.DiagnosticsAsync()).Single(r => r.Id == id); Assert.That(state.Paused, Is.True); Assert.That(state.Rate, Is.EqualTo(1.3).Within(.02));
        foreach (var offset in new[] { source.IndexOf("Third", StringComparison.Ordinal), source.IndexOf("First", StringComparison.Ordinal), source.IndexOf("Second", StringComparison.Ordinal) }) speech.Seek(id, source, offset, true, true);
        await Task.Delay(200); Assert.That((await speech.DiagnosticsAsync()).Count(r => r.HasPlayer), Is.EqualTo(1)); speech.Cancel(id); await Task.Delay(100); Assert.That(await speech.DiagnosticsAsync(), Is.Empty); Assert.That(errors, Is.Empty);
    }
    [Test]
    public async Task SuffixFallbackCanSeekBackwardWithoutDuplicatePlayers()
    {
        var voice = (await SpeechService.ListVoicesAsync()).First(v => v.Backend == "sapi"); using var speech = new SpeechService(true); var source = "First sentence contains a few repeated words. Second sentence says something else. Third sentence ends."; var errors = new List<string>(); speech.Error += errors.Add;
        var id = speech.Submit(SpeechText.Prepare(source), new(voice.Id, 0, 0, Reader: true)); await WaitForPlayer(speech, id); speech.Pause(id, source, true);
        speech.Seek(id, source, source.IndexOf("Third", StringComparison.Ordinal), false, true); await WaitForRevision(speech, id, 1); speech.Seek(id, source, 0, false, true); await WaitForRevision(speech, id, 2);
        var state = (await speech.DiagnosticsAsync()).Single(); Assert.That(state.Paused, Is.True); Assert.That(state.SourceOffset, Is.Zero); Assert.That(errors, Is.Empty);
    }
    [Test]
    public async Task TargetedCancellationPreservesUnrelatedVoiceAndLatestQueue()
    {
        var voice = (await SpeechService.ListVoicesAsync()).First(v => v.Backend == "winrt"); using var speech = new SpeechService(); var doc = SpeechText.Prepare("A passage long enough to remain active while another request is tested and canceled.");
        var one = speech.Submit(doc, new(voice.Id, 0, 0, Mode: "overlap")); var two = speech.Submit(doc, new(voice.Id, 0, 0, Mode: "overlap")); await WaitForPlayer(speech, two); speech.Cancel(two); await Task.Delay(150); Assert.That((await speech.DiagnosticsAsync()).Select(s => s.Id), Is.EqualTo(new[] { one }));
        var old = speech.Submit(doc, new(voice.Id, 0, 0, Mode: "queue")); var latest = speech.Submit(doc, new(voice.Id, 0, 0, Mode: "queue")); speech.Cancel(one); await WaitForPlayer(speech, latest); Assert.That((await speech.DiagnosticsAsync()).Any(s => s.Id == old), Is.False); speech.StopAll(); await Task.Delay(100); Assert.That(await speech.DiagnosticsAsync(), Is.Empty);
    }
    [Test]
    public async Task LongSavedSapiVoiceIsContinuousAndRepeatUsesCache()
    {
        var voices = await SpeechService.ListVoicesAsync(); var saved = File.Exists(ConfigStore.DefaultPath) ? SettingsCodec.Read(File.ReadAllText(ConfigStore.DefaultPath)).Voice : "";
        var voice = voices.FirstOrDefault(v => v.Backend == "sapi" && saved.EndsWith(v.Id[5..], StringComparison.OrdinalIgnoreCase)) ?? voices.First(v => v.Backend == "sapi"); var source = string.Join("\n\n", Enumerable.Repeat("Empirical evidence: Ideas must be backed up by observable, measurable data. Science is never finished; it changes when new evidence arrives.", 12)); var document = SpeechText.Prepare(source); using var speech = new SpeechService();
        var clock = Stopwatch.StartNew(); var first = speech.Submit(document, new(voice.Id, 0, 0, PlaybackRate: .9, Reader: true)); await WaitForPlayer(speech, first); TestContext.Out.WriteLine($"{voice.Name} long passage ready: {clock.Elapsed.TotalMilliseconds:0} ms"); Assert.That((await speech.DiagnosticsAsync()).Single().Cached, Is.False);
        clock.Restart(); var repeat = speech.Submit(document, new(voice.Id, 0, 0, PlaybackRate: 1.2, Reader: true)); await WaitForPlayer(speech, repeat); TestContext.Out.WriteLine($"Cached repeat ready: {clock.Elapsed.TotalMilliseconds:0} ms"); var state = (await speech.DiagnosticsAsync()).Single(); Assert.That(state.Cached, Is.True); Assert.That(state.Rate, Is.EqualTo(1.2).Within(.02));
    }
    [Test]
    public async Task RapidSuffixSeeksCoalesceAndPublishOnlyCurrentPlaybackRevision()
    {
        var voice = (await SpeechService.ListVoicesAsync()).First(v => v.Backend == "sapi"); using var speech = new SpeechService(true);
        const string source = "First sentence explains the first subject. Second sentence repeats repeated words. Third sentence is the latest target.";
        var preparations = 0; var states = new System.Collections.Concurrent.ConcurrentQueue<PlaybackState>(); var rates = new System.Collections.Concurrent.ConcurrentQueue<SpeechRateResult>();
        speech.Preparing += (_, _) => Interlocked.Increment(ref preparations); speech.StateChanged += states.Enqueue; speech.RateResult += rates.Enqueue;
        var id = speech.Submit(SpeechText.Prepare(source), new(voice.Id, 0, 0, Reader: true)); await WaitForPlayer(speech, id); speech.Pause(id, source, true); await Task.Delay(80);
        long latest = 0; for (int i = 0; i < 24; i++) latest = speech.Seek(id, source, i % 2 == 0 ? 0 : source.IndexOf("Third", StringComparison.Ordinal), true, true);
        await WaitForRevision(speech, id, 1); Assert.That((await speech.DiagnosticsAsync()).Single().SourceOffset, Is.EqualTo(source.IndexOf("Third", StringComparison.Ordinal))); Assert.That((await speech.DiagnosticsAsync()).Single().Paused, Is.True);
        Assert.That(preparations, Is.LessThan(8), "Obsolete clicks must not each synthesize a full suffix."); Assert.That(states.Last().PlaybackRevision, Is.GreaterThanOrEqualTo(latest)); Assert.That((await speech.DiagnosticsAsync()).Count(s => s.HasPlayer), Is.EqualTo(1));
        speech.SetRate(id, source, .8); var rateRevision = speech.SetRate(id, source, 1.2); await Task.Delay(100); Assert.That(rates.Last().Revision, Is.EqualTo(rateRevision)); Assert.That(rates.Last().Actual, Is.EqualTo(1.2).Within(.02));
    }
    [TestCase("winrt", false)]
    [TestCase("sapi", false)]
    [TestCase("winrt", true)]
    [TestCase("sapi", true)]
    public async Task StructuredNativeOffsetsTrackWordsBeyondEveryInsertedPause(string backend, bool supplementaryUnicode)
    {
        var voices = await SpeechService.ListVoicesAsync();
        var saved = File.Exists(ConfigStore.DefaultPath) ? SettingsCodec.Read(File.ReadAllText(ConfigStore.DefaultPath)).Voice : "";
        var voice = voices.FirstOrDefault(v => v.Backend == backend && v.Id == saved) ?? voices.First(v => v.Backend == backend);
        var source = "# " + (supplementaryUnicode ? "🌱 " : "") + "Science & evidence\n\n1. **Observation:** Notice repeated words repeated words.\n2. **Question:** Why do plants grow?\n\n- **Evidence:** Keep science clear.\n- **Results:** Check each conclusion.\n\nFinal paragraph follows.";
        var document = SpeechText.Prepare(source, true); using var speech = new SpeechService();
        var traced = new TaskCompletionSource<(SynthesisInput input, IReadOnlyList<SpeechService.Timing> raw)>(TaskCreationOptions.RunContinuationsAsynchronously);
        speech.BoundaryTrace += (_, input, timings) => traced.TrySetResult((input, timings));
        var events = new System.Collections.Concurrent.ConcurrentQueue<SpeechProgress>(); speech.Progress += events.Enqueue;
        var errors = new System.Collections.Concurrent.ConcurrentQueue<string>(); speech.Error += errors.Enqueue;
        var id = speech.Submit(document, new(voice.Id, 3, 0, Reader: true)); await WaitForPlayer(speech, id);
        for (var attempt = 0; attempt < 150 && events.Count < 2; attempt++) await Task.Delay(20);
        Assert.That(events.Count, Is.GreaterThanOrEqualTo(2), "A leading decorative symbol must not prevent normal playback progress.");
        Assert.That(events.Select(p => p.WordIndex).Take(2), Is.EqualTo(new[] { 0, 1 }));
        speech.Pause(id, source, true);
        var trace = await traced.Task.WaitAsync(TimeSpan.FromSeconds(30));
        TestContext.Out.WriteLine(backend + " raw: " + string.Join(" | ", trace.raw.Take(16).Select(t => $"{t.Seconds:F2}@{t.Start}:{t.End}=" + trace.input.Text[t.Start..t.End])));
        var mapped = SpeechService.MapTimings(trace.raw.ToList(), trace.input, document);
        Assert.That(mapped.Select(t => t.WordIndex).Distinct().Count(), Is.GreaterThanOrEqualTo((int)(document.Words.Count * .9)), "Almost all document words need usable native timing, not just the first heading.");
        // Validate actual native word text against mapped spoken text. Markup positions cannot be guessed from early events.
        foreach (var native in trace.raw)
        {
            var word = SpeechService.MapTimings([native], trace.input, document).FirstOrDefault();
            if (word == null) continue;
            var expected = document.Words[word.WordIndex];
            Assert.That(System.Net.WebUtility.HtmlDecode(trace.input.Text[native.Start..native.End]), Does.Contain(document.SpokenText[expected.SpokenStart..expected.SpokenEnd]).IgnoreCase, $"Native offset {native.Start}:{native.End} mapped to word {word.WordIndex}.");
        }
        foreach (var index in new[] { 5, document.Words.Count - 3, 2, 12 })
        {
            var word = document.Words[index]; var control = speech.Seek(id, source, word.SourceStart, true, true);
            for (var attempt = 0; attempt < 100 && !events.Any(p => p.PlaybackRevision >= control && p.WordIndex == index); attempt++) await Task.Delay(10);
            Assert.That(events.Any(p => p.PlaybackRevision >= control && p.WordIndex == index && p.SourceStart == word.SourceStart && p.SourceEnd == word.SourceEnd), Is.True);
            Assert.That((await speech.DiagnosticsAsync()).Single().Paused, Is.True);
        }
        Assert.That(errors, Is.Empty);
    }
    private static async Task WaitForPlayer(SpeechService speech, long id) { for (int i = 0; i < 300; i++) { if ((await speech.DiagnosticsAsync()).Any(s => s.Id == id && s.HasPlayer && s.Ready)) return; await Task.Delay(100); } Assert.Fail("The speech player did not become ready."); }
    [Test]
    public async Task ReaderCacheInvalidatesForVoiceRateVolumeTextAndPausesButNotPlaybackSpeed()
    {
        var voices = await SpeechService.ListVoicesAsync(); var voice = voices.First(v => v.Backend == "sapi");
        using var speech = new SpeechService(false, true); // Native wave may contain sound; physical output remains muted.
        var document = SpeechText.Prepare("Evidence supports science. A second sentence supplies more context.");
        var options = new SpeechOptions(voice.Id, 0, 40, Reader: true);
        async Task Check(SpeechDocument doc, SpeechOptions selected, bool cached)
        {
            var id = speech.Submit(doc, selected); await WaitForPlayer(speech, id);
            Assert.That((await speech.DiagnosticsAsync()).Single().Cached, Is.EqualTo(cached));
        }
        await Check(document, options, false); await Check(document, options with { PlaybackRate = 1.4 }, true);
        options = options with { Rate = 1 }; await Check(document, options, false);
        options = options with { Volume = 41 }; await Check(document, options, false);
        var otherVoice = voices.First(v => v.Id != voice.Id); options = options with { Voice = otherVoice.Id }; await Check(document, options, false);
        document = SpeechText.Prepare(document.SourceText + " More evidence."); await Check(document, options, false);
        document = document with { Pauses = [new SpeechPause(document.Words[3].SpokenStart, document.Words[3].SourceStart, 250)] };
        await Check(document, options, false); await Check(document, options, true);
    }
    private static async Task WaitForRevision(SpeechService speech, long id, int revision) { for (int i = 0; i < 300; i++) { if ((await speech.DiagnosticsAsync()).Any(s => s.Id == id && s.Revision >= revision && s.HasPlayer && s.Ready)) return; await Task.Delay(100); } Assert.Fail("The replacement speech stream did not become ready."); }
}
