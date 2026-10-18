using System.Diagnostics;
using System.Drawing;
using GameTextReader.Core;

namespace GameTextReader.Windows;

public sealed record CaptureResult(CorrectionResult Correction, RecognitionResult Recognition, double CaptureMs, double CorrectionMs);
public interface ICaptureService
{
    bool ManualPending { get; }
    Task<CaptureResult> ReadAsync(int[] box, AppSettings settings, bool manual, CancellationToken cancellation, Bitmap? supplied = null);
}
public sealed class CaptureService : ICaptureService
{
    private readonly OcrService ocr = new(); private readonly OcrCorrector corrector;
    private readonly SemaphoreSlim gate = new(1); private readonly object workLock = new(); private CancellationTokenSource? autoWork; private int manualPending;
    public bool ManualPending => Volatile.Read(ref manualPending) > 0;
    public CaptureService(OcrCorrector corrector) => this.corrector = corrector;
    public async Task<CaptureResult> ReadAsync(int[] box, AppSettings settings, bool manual, CancellationToken cancellation, Bitmap? supplied = null)
    {
        if (manual) { Interlocked.Increment(ref manualPending); lock (workLock) autoWork?.Cancel(); }
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellation);
        if (!manual) { if (ManualPending) throw new OperationCanceledException(); lock (workLock) autoWork = linked; }
        bool entered = false;
        try
        {
            await gate.WaitAsync(linked.Token); entered = true;
            if (!manual && ManualPending) throw new OperationCanceledException();
            return await Task.Run(async () =>
            {
                var watch = Stopwatch.StartNew(); using var image = supplied == null ? NativeDesktop.Capture(box) : new Bitmap(supplied); var captureMs = watch.Elapsed.TotalMilliseconds;
                var recognized = await ocr.RecognizeAsync(image, settings.Ocr.RecognitionMode, linked.Token); linked.Token.ThrowIfCancellationRequested(); watch.Restart(); var result = corrector.Correct(recognized.Text, settings.Ocr); linked.Token.ThrowIfCancellationRequested(); return new CaptureResult(result, recognized, captureMs, watch.Elapsed.TotalMilliseconds);
            }, linked.Token);
        }
        finally { if (entered) gate.Release(); if (manual) Interlocked.Decrement(ref manualPending); else lock (workLock) if (autoWork == linked) autoWork = null; }
    }
}
public sealed class AutoReadWatcher : IDisposable
{
    private readonly ICaptureService capture; private readonly Func<AppSettings> settings; private readonly AutoReadEnvironment environment; private CancellationTokenSource? session; private DialogueGate gate = new(); private readonly object gateLock = new(); private long generation, epoch;
    public bool Active => session is { IsCancellationRequested: false };
    public event Action<string, bool>? Status; public event Action<long, long, CaptureResult>? Line; public event Action<long, long>? CancelGrowth;
    public AutoReadWatcher(ICaptureService capture, Func<AppSettings> settings, AutoReadEnvironment? environment = null) { this.capture = capture; this.settings = settings; this.environment = environment ?? new(); }
    public void Start(CaptureArea area)
    {
        Stop(); DialogueGate runGate; lock (gateLock) runGate = gate = new(); var next = new CancellationTokenSource(); session = next; var id = Interlocked.Increment(ref generation); _ = Run(area, next, id, runGate);
    }
    public void Stop() { var previous = session; session = null; try { previous?.Cancel(); } catch (ObjectDisposedException) { } Interlocked.Increment(ref generation); }
    public bool IsCurrent(long id, long revision) => Active && generation == id && epoch == revision;
    public void ManualStarted() { Interlocked.Increment(ref epoch); lock (gateLock) gate.ResetCandidate(); }
    public void RememberManual(string text) { lock (gateLock) gate.RememberManual(text); }
    private async Task Run(CaptureArea area, CancellationTokenSource run, long id, DialogueGate runGate)
    {
        var token = run.Token; (nint handle, uint pid)? target = null; var armed = environment.Now(); byte[]? previous = null; long changed = 0, scanned = 0; int errors = 0; string speed = ""; bool wasPaused = false, failed = false;
        try
        {
            while (!token.IsCancellationRequested)
            {
                var snapshot = settings(); var policy = AutoReadPolicy.For(snapshot.AutoRead.Speed);
                if (speed != policy.Speed) { speed = policy.Speed; lock (gateLock) runGate.ResetCandidate(); Status?.Invoke($"Auto-Read {speed}: waiting for stable dialogue.", false); }
                var box = CaptureGeometry.Resolve(area, environment.Monitors());
                var foreground = environment.Foreground(box);
                if (target == null)
                {
                    if (foreground != null) { target = foreground; Status?.Invoke($"Auto-Read {speed}: watching the game.", false); }
                    else if (environment.Now() - armed > 30000) throw new InvalidOperationException("No foreground game covered the capture area within 30 seconds.");
                }
                if (target != null && !environment.Exists(target.Value.handle)) throw new InvalidOperationException("The watched game window closed.");
                if (target != null && environment.Bounds(target.Value.handle) is { } bounds && (bounds[0] > box[0] || bounds[1] > box[1] || bounds[2] < box[2] || bounds[3] < box[3])) throw new InvalidOperationException("The watched game no longer covers the capture area.");
                if (target == null || foreground != target || capture.ManualPending)
                {
                    if (!wasPaused) { Status?.Invoke($"Auto-Read {speed}: paused while the game is not active.", false); wasPaused = true; lock (gateLock) runGate.ResetCandidate(); previous = null; }
                    await environment.Delay(policy.PollMs, token); continue;
                }
                if (wasPaused) { Status?.Invoke($"Auto-Read {speed}: watching the game.", false); wasPaused = false; }
                try
                {
                    using var image = await Task.Run(() => environment.Grab(box), token); var fingerprint = OcrService.Fingerprint(image); var now = environment.Now();
                    if (previous == null || OcrService.Difference(previous, fingerprint) >= .8) { previous = fingerprint; changed = now; scanned = 0; }
                    if (now - changed < policy.SettleMs || now - scanned < policy.ConfirmMs) { await environment.Delay(policy.PollMs, token); continue; }
                    bool enough; lock (gateLock) enough = runGate.Confirmations >= 2 && !runGate.Recovering;
                    if (enough && now - scanned < policy.IdleMs) { await environment.Delay(policy.PollMs, token); continue; }
                    var capturedEpoch = epoch;
                    var result = await capture.ReadAsync(box, snapshot, false, token, image); token.ThrowIfCancellationRequested(); scanned = environment.Now(); errors = 0;
                    if (!IsCurrent(id, capturedEpoch)) continue;
                    // OCR can finish after the user Alt-Tabs. Do not consume the
                    // dialogue in the duplicate gate or publish background speech.
                    if (environment.Foreground(box) != target || capture.ManualPending)
                    {
                        lock (gateLock) runGate.ResetCandidate();
                        previous = null; continue;
                    }
                    if (settings().AutoRead.Speed != snapshot.AutoRead.Speed) { lock (gateLock) runGate.ResetCandidate(); continue; }
                    DialogueDecision decision; lock (gateLock) decision = runGate.Observe(result.Correction.CorrectedText, policy, scanned);
                    if (decision == DialogueDecision.CancelGrowth) CancelGrowth?.Invoke(id, capturedEpoch); else if (decision == DialogueDecision.Speak) Line?.Invoke(id, capturedEpoch, result);
                }
                catch (OperationCanceledException) when (!token.IsCancellationRequested) { previous = null; }
                catch (Exception e) when (e is not OperationCanceledException) { errors++; if (errors >= 3) throw new InvalidOperationException("Auto-Read stopped after repeated errors: " + e.Message); Status?.Invoke("Auto-Read retrying: " + e.Message, true); await environment.Delay(errors == 1 ? 2000 : 4000, token); }
                await environment.Delay(policy.PollMs, token);
            }
        }
        catch (OperationCanceledException) { }
        catch (Exception e) { if (session == run) { failed = true; run.Cancel(); session = null; Status?.Invoke(e.Message, true); } }
        finally { if (session == run) { session = null; if (!failed) Status?.Invoke("Auto-Read is off.", false); } run.Dispose(); }
    }
    public void Dispose() => Stop();
}
public sealed class AutoReadEnvironment
{
    public Func<long> Now { get; init; } = () => Environment.TickCount64;
    public Func<int, CancellationToken, Task> Delay { get; init; } = Task.Delay;
    public Func<IReadOnlyList<MonitorInfo>> Monitors { get; init; } = NativeDesktop.Monitors;
    public Func<int[], (nint handle, uint pid)?> Foreground { get; init; } = NativeDesktop.Foreground;
    public Func<nint, bool> Exists { get; init; } = NativeDesktop.IsWindow;
    public Func<nint, int[]?> Bounds { get; init; } = h => NativeDesktop.GetWindowRect(h, out var r) ? [r.Left, r.Top, r.Right, r.Bottom] : null;
    public Func<int[], Bitmap> Grab { get; init; } = NativeDesktop.Capture;
}
