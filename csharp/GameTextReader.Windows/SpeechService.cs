using System.Diagnostics;
using GameTextReader.Core;
using Windows.Media.Core;
using Windows.Media.Playback;
using Windows.Storage.Streams;
using WinSynth = Windows.Media.SpeechSynthesis.SpeechSynthesizer;

namespace GameTextReader.Windows;

public sealed record VoiceInfo(string Id, string Name, string Backend);
public sealed record SpeechProgress(long RequestId, string SourceText, int SourceStart, int SourceEnd, int WordIndex, long PlaybackRevision = 0);
public sealed record PlaybackState(long RequestId, string SourceText, bool Paused, bool HasTiming, int SourceOffset, double Rate, long PlaybackRevision = 0);
public sealed record SpeechRateResult(long RequestId, long Revision, double Requested, double Actual, string? Error);
public sealed record SpeechOptions(string Voice, int Rate, double Volume, string Mode = "replace", int MaxOverlap = 2, double PlaybackRate = 1, bool Reader = false, bool Preview = false);
public sealed record SpeechDiagnostic(long Id, bool HasPlayer, bool Paused, bool HasTiming, double Rate, int SourceOffset, int Revision, bool Cached, bool Ready, double PositionSeconds = 0, double DurationSeconds = 0);
public sealed class SpeechService : IDisposable
{
    internal sealed record Timing(double Seconds, int Start, int End, int WordIndex = -1);
    private sealed record Audio(byte[] Bytes, Timing[] Timings);
    private sealed class Request(long id, SpeechDocument document, SpeechOptions options)
    {
        public long Id = id; public SpeechDocument Document = document; public readonly SpeechDocument FullDocument = document; public SpeechOptions Options = options;
        public readonly CancellationTokenSource Cancel = new(); public MediaPlayer? Player; public InMemoryRandomAccessStream? Stream; public NativeSapiVoice? Sapi; public WinSynth? Winrt;
        public Timing[] Timings = []; public int[] FullWordIndices = []; public int TimingCursor = -1; public int Revision, LastWord = -1; public bool Paused, Started, Finished, RateReady, Cached; public long StartTicks, PlaybackRevision, RateRevision; public int SourceOffset; public DateTime PreparingSince = DateTime.UtcNow;
    }
    private readonly Thread thread; private readonly TaskCompletionSource<System.Windows.Forms.Control> ready = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly Dictionary<long, Request> active = []; private readonly System.Windows.Forms.Timer poll = new() { Interval = 20 };
    private readonly List<(MediaPlayer player, InMemoryRandomAccessStream? stream, long due)> retired = [];
    private readonly List<Request> overlapWaiting = [];
    private readonly List<(string voice, NativeSapiVoice synth)> sapiPool = []; private readonly List<(string voice, WinSynth synth)> winrtPool = [];
    private readonly HashSet<Task> starting = [];
    private Request? queued; private long nextId, nextControlRevision; private bool disposed, stopping; private (string key, Audio audio)? cache;
    private readonly System.Collections.Concurrent.ConcurrentDictionary<long, (long revision, int offset, bool stopOthers, bool keepPaused)> seeks = new();
    public event Action<long, string>? Preparing; public event Action<long, string>? Started; public event Action<long, string>? Finished;
    public event Action<SpeechProgress>? Progress; public event Action<PlaybackState>? StateChanged; public event Action<string>? Error; public event Action<SpeechRateResult>? RateResult;
    internal event Action<string, SynthesisInput, IReadOnlyList<Timing>>? BoundaryTrace;
    private readonly bool forceSuffixSeeking, muteOutput;
    public SpeechService(bool forceSuffixSeeking = false) : this(forceSuffixSeeking, false) { }
    internal SpeechService(bool forceSuffixSeeking, bool muteOutput)
    {
        this.forceSuffixSeeking = forceSuffixSeeking; this.muteOutput = muteOutput;
        thread = new(() => { var control = new System.Windows.Forms.Control(); control.CreateControl(); _ = control.Handle; poll.Tick += (_, _) => Poll(); poll.Start(); ready.SetResult(control); System.Windows.Forms.Application.Run(); poll.Dispose(); control.Dispose(); }) { IsBackground = true, Name = "Windows speech" };
        thread.SetApartmentState(ApartmentState.STA); thread.Start();
    }
    public Task<IReadOnlyList<SpeechDiagnostic>> DiagnosticsAsync()
    {
        var result = new TaskCompletionSource<IReadOnlyList<SpeechDiagnostic>>(TaskCreationOptions.RunContinuationsAsynchronously); Post(() => result.SetResult(active.Values.Select(r => new SpeechDiagnostic(r.Id, r.Player != null, r.Paused, r.Timings.Length > 0, r.Player?.PlaybackSession.PlaybackRate ?? r.Options.PlaybackRate, r.SourceOffset, r.Revision, r.Cached, r.RateReady, r.Player?.PlaybackSession.Position.TotalSeconds ?? 0, r.Player?.PlaybackSession.NaturalDuration.TotalSeconds ?? 0)).ToArray())); return result.Task;
    }
    private void Post(Action action)
    {
        if (disposed) return;
        Action guarded = () => { try { action(); } catch (Exception e) { Error?.Invoke("Speech worker: " + e.Message); } };
        if (ready.Task.IsCompletedSuccessfully) { try { ready.Task.Result.BeginInvoke(guarded); } catch (InvalidOperationException) { } }
        else _ = ready.Task.ContinueWith(t => { if (!disposed && !t.Result.IsDisposed) try { t.Result.BeginInvoke(guarded); } catch (InvalidOperationException) { } }, TaskScheduler.Default);
    }
    public static Task<IReadOnlyList<VoiceInfo>> ListVoicesAsync() => Task.Run<IReadOnlyList<VoiceInfo>>(() =>
    {
        var result = new List<VoiceInfo>();
        foreach (var v in WinSynth.AllVoices) result.Add(new("winrt:" + v.Id, v.DisplayName + " (Windows)", "winrt"));
        foreach (var token in SapiTokens()) result.Add(new("sapi:" + token.id, token.name + " (SAPI)", "sapi"));
        return result;
    });
    private sealed record SapiToken(string id, string name);
    private static List<SapiToken> SapiTokens()
    {
        var result = new List<SapiToken>(); object? speaker = null, collection = null;
        try
        {
            var type = Type.GetTypeFromProgID("SAPI.SpVoice"); if (type == null) return result;
            speaker = Activator.CreateInstance(type); collection = ((dynamic)speaker!).GetVoices();
            for (int i = 0; i < ((dynamic)collection).Count; i++)
            {
                object token = ((dynamic)collection).Item(i);
                try { result.Add(new((string)((dynamic)token).Id, (string)((dynamic)token).GetDescription())); }
                finally { System.Runtime.InteropServices.Marshal.ReleaseComObject(token); }
            }
        }
        catch (Exception e) when (e is System.Runtime.InteropServices.COMException or InvalidCastException) { }
        finally { if (collection != null) System.Runtime.InteropServices.Marshal.ReleaseComObject(collection); if (speaker != null) System.Runtime.InteropServices.Marshal.ReleaseComObject(speaker); }
        return result;
    }
    public long Submit(SpeechDocument document, SpeechOptions options)
    {
        ObjectDisposedException.ThrowIf(stopping || disposed, this);
        var request = new Request(Interlocked.Increment(ref nextId), document, options);
        Post(() =>
        {
            if (options.Reader) foreach (var previous in active.Values.Where(r => r.Options.Reader).ToArray()) End(previous);
            else if (options.Preview) foreach (var previous in active.Values.Where(r => r.Options.Preview).ToArray()) End(previous);
            else if (options.Mode == "queue" && active.Values.Any(r => !r.Options.Preview)) { var old = queued; queued = null; if (old != null) { old.Cancel.Cancel(); Finished?.Invoke(old.Id, old.FullDocument.SourceText); } queued = request; return; }
            else if (options.Mode == "replace") { ClearPending(); foreach (var previous in active.Values.Where(r => !r.Options.Preview).ToArray()) End(previous); }
            else if (options.Mode == "overlap" && active.Values.Count(r => !r.Options.Preview) >= options.MaxOverlap) { if (overlapWaiting.Count >= options.MaxOverlap) { var old = overlapWaiting[0]; overlapWaiting.RemoveAt(0); old.Cancel.Cancel(); Finished?.Invoke(old.Id, old.FullDocument.SourceText); } overlapWaiting.Add(request); return; }
            active[request.Id] = request; StartTracked(request);
        });
        return request.Id;
    }
    public void Cancel(long id) => Post(() => { if (queued?.Id == id) { var old = queued; queued = null; CancelPending(old); } foreach (var pending in overlapWaiting.Where(r => r.Id == id).ToArray()) { overlapWaiting.Remove(pending); CancelPending(pending); } if (active.TryGetValue(id, out var r)) End(r); });
    public void StopAll() => Post(() => { ClearPending(); foreach (var r in active.Values.ToArray()) End(r); });
    private void CancelPending(Request r) { if (r.Finished) return; r.Finished = true; r.Cancel.Cancel(); Finished?.Invoke(r.Id, r.FullDocument.SourceText); }
    private void ClearPending() { var old = queued; queued = null; if (old != null) CancelPending(old); var waiting = overlapWaiting.ToArray(); overlapWaiting.Clear(); foreach (var r in waiting) CancelPending(r); }
    public long Pause(long id, string source, bool paused)
    {
        var revision = Interlocked.Increment(ref nextControlRevision);
        Post(() => { if (Match(id, source) is not { } r) return; r.PlaybackRevision = Math.Max(r.PlaybackRevision, revision); try { if (paused) r.Player?.Pause(); else r.Player?.Play(); r.Paused = paused; r.StartTicks = Environment.TickCount64; Notify(r); } catch (Exception e) { Error?.Invoke("Playback control failed: " + e.Message); } });
        return revision;
    }
    public long SetRate(long id, string source, double rate)
    {
        var revision = Interlocked.Increment(ref nextControlRevision);
        Post(() =>
        {
            if (Match(id, source) is not { } r) return; var old = r.Options.PlaybackRate; r.RateRevision = revision;
            try { if (r.Player != null) { r.Player.PlaybackSession.PlaybackRate = rate; if (Math.Abs(r.Player.PlaybackSession.PlaybackRate - rate) > .02) throw new InvalidOperationException("The voice stream rejected this speed."); } r.Options = r.Options with { PlaybackRate = rate }; r.StartTicks = Environment.TickCount64; RateResult?.Invoke(new(id, revision, rate, rate, null)); Notify(r); }
            catch (Exception e) { try { if (r.Player != null) r.Player.PlaybackSession.PlaybackRate = old; } catch { } RateResult?.Invoke(new(id, revision, rate, old, e.Message)); }
        });
        return revision;
    }
    public long Seek(long id, string source, int sourceOffset, bool stopOthers, bool keepPaused)
    {
        var revision = Interlocked.Increment(ref nextControlRevision); seeks[id] = (revision, sourceOffset, stopOthers, keepPaused);
        Post(() => { if (seeks.TryRemove(id, out var target)) SeekOnWorker(id, source, target.offset, target.stopOthers, target.keepPaused, target.revision); });
        return revision;
    }
    private void SeekOnWorker(long id, string source, int sourceOffset, bool stopOthers, bool keepPaused, long playbackRevision)
    {
        if (Match(id, source) is not { } r) return;
        r.PlaybackRevision = Math.Max(r.PlaybackRevision, playbackRevision);
        if (stopOthers) { ClearPending(); foreach (var other in active.Values.Where(x => x != r).ToArray()) End(other); }
        var word = r.FullDocument.Words.FirstOrDefault(w => w.SourceEnd > sourceOffset); if (word == null) return;
        var streamWord = r.Document.Words.FirstOrDefault(w => w.SourceStart == word.SourceStart && w.SourceEnd == word.SourceEnd);
        var timing = streamWord == null ? null : r.Timings.FirstOrDefault(t => t.End > streamWord.SpokenStart); var paused = keepPaused && r.Paused;
        try
        {
            if (!forceSuffixSeeking && r.Player?.PlaybackSession.CanSeek == true && timing != null)
            {
                r.Player.PlaybackSession.Position = TimeSpan.FromSeconds(timing.Seconds); r.LastWord = -1; r.TimingCursor = -1; r.SourceOffset = word.SourceStart; r.StartTicks = Environment.TickCount64; r.Paused = paused; if (!paused) r.Player.Play(); else r.Player.Pause(); Notify(r); EmitWord(r, word); return;
            }
        }
        catch (Exception) { /* Regenerate the mapped suffix if the backend cannot seek. */ }
        RetirePlayer(r); r.Revision++; r.Sapi?.Cancel(); r.Document = r.FullDocument.FromSource(sourceOffset); r.Timings = []; r.LastWord = -1; r.TimingCursor = -1; r.Paused = paused; r.Started = false; r.SourceOffset = sourceOffset;
        var synthesisRevision = r.Revision;
        Post(async () => { await Task.Delay(40); if (!r.Cancel.IsCancellationRequested && !stopping && r.Revision == synthesisRevision) StartTracked(r); });
    }
    private Request? Match(long id, string source) => active.TryGetValue(id, out var r) && !r.Cancel.IsCancellationRequested && r.FullDocument.SourceText == source ? r : null;
    private void StartTracked(Request r) { var task = StartAsync(r); starting.Add(task); _ = task.ContinueWith(_ => Post(() => starting.Remove(task)), TaskScheduler.Default); }
    private async Task StartAsync(Request r)
    {
        int revision = r.Revision; var document = r.Document;
        try
        {
            if (r.Document.SpokenText.Length == 0) { End(r); return; }
            Preparing?.Invoke(r.Id, r.FullDocument.SourceText); r.PreparingSince = DateTime.UtcNow;
            var key = $"{r.Options.Voice}\0{r.Options.Rate}\0{r.Options.Volume}\0{r.Document.SpokenText}\0{string.Join(';', r.Document.Pauses.Select(p => $"{p.SpokenOffset}:{p.Milliseconds}"))}";
            Audio audio;
            if (r.Options.Reader && cache?.key == key) { audio = cache.Value.audio; r.Cached = true; }
            else
            {
                audio = r.Options.Voice.StartsWith("sapi:") ? await SynthesizeSapi(r, document, revision) : await SynthesizeWinrt(r, document, revision);
                if (!r.Cancel.IsCancellationRequested && r.Revision == revision && r.Options.Reader && audio.Bytes.Length <= 16 * 1024 * 1024) cache = (key, audio);
            }
            if (r.Cancel.IsCancellationRequested || r.Revision != revision || disposed) return;
            var stream = new InMemoryRandomAccessStream(); using (var writer = new DataWriter(stream)) { writer.WriteBytes(audio.Bytes); await writer.StoreAsync(); writer.DetachStream(); }
            stream.Seek(0);
            if (r.Cancel.IsCancellationRequested || r.Revision != revision) { stream.Dispose(); return; }
            r.Timings = audio.Timings; r.TimingCursor = -1;
            var fullWords = r.FullDocument.Words.Select((word, index) => (word, index)).ToDictionary(p => (p.word.SpokenStart, p.word.SourceStart, p.word.SourceEnd), p => p.index);
            var shift = document.Words.Count == 0 ? 0 : (r.FullDocument.Words.FirstOrDefault(w => w.SourceStart == document.Words[0].SourceStart && w.SourceEnd == document.Words[0].SourceEnd)?.SpokenStart ?? document.Words[0].SpokenStart) - document.Words[0].SpokenStart;
            r.FullWordIndices = document.Words.Select(word => fullWords.GetValueOrDefault((word.SpokenStart + shift, word.SourceStart, word.SourceEnd), -1)).ToArray();
            r.Stream = stream;
            var player = new MediaPlayer { AutoPlay = false, Volume = 0, IsLoopingEnabled = false }; player.CommandManager.IsEnabled = false;
            r.Player = player; r.RateReady = false;
            player.MediaOpened += (_, _) => Post(() => { if (r.Player != player || r.Cancel.IsCancellationRequested || r.Revision != revision) return; ApplyInitialRate(r); });
            player.MediaEnded += (_, _) => Post(() => { if (r.Player == player && r.Revision == revision) End(r); });
            player.MediaFailed += (_, e) => Post(() => { if (r.Player == player) { Error?.Invoke("Speech playback failed: " + e.ErrorMessage); End(r); } });
            player.Source = MediaSource.CreateFromStream(r.Stream, "audio/wav");
            r.StartTicks = Environment.TickCount64;
        }
        catch (Exception e) { if (!r.Cancel.IsCancellationRequested && r.Revision == revision) { Error?.Invoke("Speech error: " + e.Message); End(r); } }
    }
    private async Task<Audio> SynthesizeWinrt(Request r, SpeechDocument document, int revision)
    {
        var pooled = winrtPool.FindIndex(p => p.voice == r.Options.Voice); var synth = pooled >= 0 ? winrtPool[pooled].synth : new WinSynth(); if (pooled >= 0) winrtPool.RemoveAt(pooled); r.Winrt = synth;
        try
        {
            if (r.Options.Voice.StartsWith("winrt:")) { var id = r.Options.Voice[6..]; synth.Voice = WinSynth.AllVoices.FirstOrDefault(v => v.Id == id) ?? throw new InvalidOperationException("The selected Windows voice is unavailable. Choose another voice."); }
            synth.Options.SpeakingRate = Math.Clamp(1 + r.Options.Rate / 10d, .25, 2); synth.Options.AudioVolume = Math.Clamp(r.Options.Volume / 100d, 0, 1); synth.Options.IncludeWordBoundaryMetadata = true;
            var input = SpeechText.Synthesis(document, false, synth.Voice.Language); global::Windows.Media.SpeechSynthesis.SpeechSynthesisStream stream;
            try { stream = input.Structured ? await synth.SynthesizeSsmlToStreamAsync(input.Text) : await synth.SynthesizeTextToStreamAsync(input.Text); }
            catch (Exception e) when (e is not OperationCanceledException && !r.Cancel.IsCancellationRequested && r.Revision == revision && input.Structured) { input = SpeechText.Synthesis(document, false, structured: false); stream = await synth.SynthesizeTextToStreamAsync(input.Text); }
            using (stream)
            {
                var timings = new List<Timing>();
                foreach (var track in stream.TimedMetadataTracks.Where(t => t.Id == "SpeechWord")) foreach (var raw in track.Cues) if (raw is SpeechCue cue && cue.StartPositionInInput is int a && cue.EndPositionInInput is int b) timings.Add(new(cue.StartTime.TotalSeconds, a, b + 1));
                BoundaryTrace?.Invoke("winrt", input, timings);
                var mapped = MapTimings(timings, input, document);
                var bytes = new byte[(int)stream.Size]; using var reader = new DataReader(stream.GetInputStreamAt(0)); await reader.LoadAsync((uint)bytes.Length); reader.ReadBytes(bytes); if (ReferenceEquals(r.Winrt, synth)) r.Winrt = null; return new(bytes, mapped);
            }
        }
        finally { if (ReferenceEquals(r.Winrt, synth)) r.Winrt = null; if (stopping || disposed || winrtPool.Count >= 2) synth.Dispose(); else winrtPool.Add((r.Options.Voice, synth)); }
    }
    private async Task<Audio> SynthesizeSapi(Request r, SpeechDocument document, int revision)
    {
        var pooled = sapiPool.FindIndex(p => p.voice == r.Options.Voice); var synth = pooled >= 0 ? sapiPool[pooled].synth : new NativeSapiVoice(r.Options.Voice[5..]); if (pooled >= 0) sapiPool.RemoveAt(pooled); r.Sapi = synth;
        try
        {
            var input = SpeechText.Synthesis(document, true); NativeSapiVoice.Result audio;
            try { audio = await synth.SynthesizeAsync(input, r.Options.Rate, r.Options.Volume, r.Cancel.Token); }
            catch (Exception e) when (e is not OperationCanceledException && !r.Cancel.IsCancellationRequested && r.Revision == revision && input.Structured)
            {
                input = SpeechText.Synthesis(document, true, structured: false);
                audio = await synth.SynthesizeAsync(input, r.Options.Rate, r.Options.Volume, r.Cancel.Token);
            }
            var timings = audio.Words.Select(word => new Timing(word.Seconds, word.Start, word.End)).ToList();
            BoundaryTrace?.Invoke("sapi", input, timings);
            return new(audio.Wave, MapTimings(timings, input, document));
        }
        finally { if (ReferenceEquals(r.Sapi, synth)) r.Sapi = null; if (stopping || disposed || sapiPool.Count >= 4) synth.Dispose(); else sapiPool.Add((r.Options.Voice, synth)); }
    }
    internal static Timing[] MapTimings(List<Timing> timings, SynthesisInput input, SpeechDocument doc)
    {
        var result = new List<Timing>(timings.Count);
        foreach (var timing in timings)
        {
            if (!double.IsFinite(timing.Seconds) || timing.Seconds < 0 || timing.Start < 0 || timing.End <= timing.Start || timing.End > input.Text.Length || timing.End >= input.NativeToSpoken.Length) continue;
            // SAPI events and WinRT cues are UTF-16 offsets into their exact synthesis input.
            var start = input.NativeToSpoken[timing.Start]; var end = input.NativeToSpoken[timing.End];
            if (start < 0 || end <= start || end > doc.SpokenText.Length) continue;
            var low = 0; var high = doc.Words.Count;
            while (low < high) { var middle = low + (high - low) / 2; if (doc.Words[middle].SpokenEnd <= start) low = middle + 1; else high = middle; }
            if (low < doc.Words.Count && doc.Words[low].SpokenStart < end) result.Add(timing with { Start = start, End = end, WordIndex = low });
        }
        return result.OrderBy(t => t.Seconds).ToArray();
    }
    private void ApplyInitialRate(Request r)
    {
        if (r.Player == null || r.RateReady) return;
        var rate = r.Options.PlaybackRate;
        try { r.Player.PlaybackSession.PlaybackRate = rate; if (Math.Abs(r.Player.PlaybackSession.PlaybackRate - rate) > .02) throw new InvalidOperationException(); }
        catch
        {
            try { r.Player.PlaybackSession.PlaybackRate = 1; }
            catch (Exception error) { Error?.Invoke("Windows could not initialize speech playback: " + error.Message); End(r); return; }
            r.Options = r.Options with { PlaybackRate = 1 }; RateResult?.Invoke(new(r.Id, r.RateRevision, rate, 1, "Windows could not apply the requested playback speed; using 1.0×."));
        }
        r.RateReady = true; r.Player.Volume = muteOutput ? 0 : 1; r.Started = true; if (!r.Paused) r.Player.Play(); r.StartTicks = Environment.TickCount64;
        Started?.Invoke(r.Id, r.FullDocument.SourceText); Notify(r);
    }
    private void Poll()
    {
        foreach (var old in retired.Where(p => p.due <= Environment.TickCount64).ToArray()) { try { old.player.Dispose(); old.stream?.Dispose(); } catch (Exception e) { Error?.Invoke("Audio cleanup: " + e.Message); } finally { retired.Remove(old); } }
        foreach (var r in active.Values.ToArray())
        {
            try
            {
                if (r.Cancel.IsCancellationRequested || r.Player == null) continue;
                if (!r.RateReady) { if (r.Player.PlaybackSession.PlaybackState is MediaPlaybackState.Paused or MediaPlaybackState.Playing || Environment.TickCount64 - r.StartTicks > 1000) ApplyInitialRate(r); continue; }
                if (r.Paused) continue;
                var seconds = r.Player.PlaybackSession.Position.TotalSeconds;
                while (r.TimingCursor + 1 < r.Timings.Length && r.Timings[r.TimingCursor + 1].Seconds <= seconds + .015) r.TimingCursor++;
                if (r.TimingCursor >= 0)
                {
                    var index = r.Timings[r.TimingCursor].WordIndex;
                    if (index >= 0 && index != r.LastWord) { r.LastWord = index; EmitWord(r, r.Document.Words[index]); }
                }
                var duration = r.Player.PlaybackSession.NaturalDuration.TotalSeconds;
                if (duration > 0 && Environment.TickCount64 - r.StartTicks > (duration / Math.Max(.5, r.Options.PlaybackRate) + 30) * 1000) { Error?.Invoke("Playback stopped responding."); End(r); }
            }
            catch (Exception e) { Error?.Invoke("Speech output failed: " + e.Message); End(r); }
        }
    }
    private void EmitWord(Request r, SpeechWord word)
    {
        if (r.Options.Preview) return; r.SourceOffset = word.SourceStart;
        var index = r.LastWord >= 0 && r.LastWord < r.FullWordIndices.Length ? r.FullWordIndices[r.LastWord] : FindFullWord(r.FullDocument, word);
        if (index < 0) return;
        Progress?.Invoke(new(r.Id, r.FullDocument.SourceText, word.SourceStart, word.SourceEnd, index, r.PlaybackRevision));
    }
    private static int FindFullWord(SpeechDocument document, SpeechWord word)
    {
        for (var i = 0; i < document.Words.Count; i++) if (document.Words[i].SourceStart == word.SourceStart && document.Words[i].SourceEnd == word.SourceEnd) return i;
        return -1;
    }
    private void Notify(Request r) => StateChanged?.Invoke(new(r.Id, r.FullDocument.SourceText, r.Paused, r.Timings.Length > 0, r.SourceOffset, r.Options.PlaybackRate, r.PlaybackRevision));
    private void RetirePlayer(Request r)
    {
        var player = r.Player; var stream = r.Stream; r.Player = null; r.Stream = null;
        if (player != null) { try { player.Volume = 0; player.Pause(); } catch (Exception e) { Error?.Invoke("Stopping audio: " + e.Message); } }
        if (player != null) retired.Add((player, stream, Environment.TickCount64 + 500)); else stream?.Dispose();
    }
    private void End(Request r)
    {
        if (r.Finished) return; r.Finished = true; r.Cancel.Cancel(); try { r.Sapi?.Cancel(); } catch (Exception e) { Error?.Invoke("Stopping synthesis: " + e.Message); }
        RetirePlayer(r); active.Remove(r.Id); Finished?.Invoke(r.Id, r.FullDocument.SourceText);
        if (queued != null && active.Values.All(x => x.Options.Preview)) { var next = queued; queued = null; active[next.Id] = next; StartTracked(next); }
        if (overlapWaiting.Count > 0 && active.Values.Count(x => !x.Options.Preview) < overlapWaiting[0].Options.MaxOverlap) { var next = overlapWaiting[0]; overlapWaiting.RemoveAt(0); active[next.Id] = next; StartTracked(next); }
    }
    public void Dispose()
    {
        if (disposed || stopping) return; stopping = true; var signal = new ManualResetEventSlim(); Post(async () => { ClearPending(); foreach (var r in active.Values.ToArray()) End(r); try { await Task.WhenAll(starting.ToArray()).WaitAsync(TimeSpan.FromSeconds(3)); } catch (Exception) { } foreach (var old in retired) { old.player.Dispose(); old.stream?.Dispose(); } retired.Clear(); foreach (var p in sapiPool) p.synth.Dispose(); foreach (var p in winrtPool) p.synth.Dispose(); sapiPool.Clear(); winrtPool.Clear(); poll.Stop(); signal.Set(); System.Windows.Forms.Application.ExitThread(); }); signal.Wait(5000); disposed = true; thread.Join(1000);
    }
}
