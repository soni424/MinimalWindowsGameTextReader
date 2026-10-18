using System.Drawing;
using System.IO;
using GameTextReader.Core;
using GameTextReader.Windows;
using NUnit.Framework;

namespace GameTextReader.Tests;

public sealed class AutoReadTests
{
    private sealed class FakeCapture : ICaptureService
    {
        public bool ManualPending { get; set; }
        public int Reads, Pending, MaxPending;
        public TaskCompletionSource<CaptureResult>? HeldResult;
        public readonly TaskCompletionSource Entered = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public static CaptureResult StableResult => new(new("Hello there.", "Hello there.", [], 0, []), new("Hello there.", "original", 1, 1), 0, 0);
        public async Task<CaptureResult> ReadAsync(int[] box, AppSettings settings, bool manual, CancellationToken cancellation, Bitmap? supplied = null)
        {
            MaxPending = Math.Max(MaxPending, ++Pending); Reads++; Entered.TrySetResult();
            try { return HeldResult == null ? StableResult : await HeldResult.Task; }
            finally { Pending--; }
        }
    }
    private sealed class Fixture
    {
        public long Time = 10000; public int Grabs, Failures; public bool Foreground = true;
        public AppSettings Settings = SettingsCodec.Normalize(new()); public readonly FakeCapture Capture = new();
        public AutoReadEnvironment Environment => new()
        {
            Now = () => Interlocked.Read(ref Time),
            Delay = async (ms, token) => { Interlocked.Add(ref Time, ms); await Task.Delay(1, token); },
            Monitors = () => [new("TEST", 0, 0, 200, 200)],
            Foreground = _ => Foreground ? (42, 77u) : null,
            Exists = _ => true,
            Bounds = _ => [0, 0, 200, 200],
            Grab = _ => { Grabs++; if (Failures > 0) { Failures--; throw new IOException("simulated screenshot failure"); } return new Bitmap(100, 30); }
        };
        public AutoReadWatcher Watcher() => new(Capture, () => Settings.Clone(), Environment);
        public static CaptureArea Area => new() { Box = [0, 0, 100, 30] };
    }
    private static async Task<long> FirstLine(string speed)
    {
        var fixture = new Fixture(); fixture.Settings.AutoRead.Speed = speed; using var watcher = fixture.Watcher(); var line = new TaskCompletionSource<long>(TaskCreationOptions.RunContinuationsAsynchronously); watcher.Line += (_, _, _) => line.TrySetResult(fixture.Time - 10000); watcher.Start(Fixture.Area); var elapsed = await line.Task.WaitAsync(TimeSpan.FromSeconds(5)); Assert.That(fixture.Capture.MaxPending, Is.EqualTo(1)); return elapsed;
    }
    [Test] public async Task FastAndInstantAcceptStableDialogueSooner() { var normal = await FirstLine("normal"); var fast = await FirstLine("fast"); var instant = await FirstLine("instant"); Assert.That(fast, Is.LessThan(normal)); Assert.That(instant, Is.LessThan(fast)); TestContext.Out.WriteLine($"Virtual stable-line detection: normal {normal} ms; fast {fast} ms; instant {instant} ms"); }
    [Test]
    public async Task RestartRearmsSameDialogueAndRejectsOldSessionEvents()
    {
        var f = new Fixture(); using var watcher = f.Watcher(); var received = new TaskCompletionSource<(long id, long revision)>(TaskCreationOptions.RunContinuationsAsynchronously); watcher.Line += (id, revision, _) => received.TrySetResult((id, revision)); watcher.Start(Fixture.Area); var old = await received.Task.WaitAsync(TimeSpan.FromSeconds(5)); Assert.That(watcher.IsCurrent(old.id, old.revision), Is.True); watcher.ManualStarted(); Assert.That(watcher.IsCurrent(old.id, old.revision), Is.False); watcher.Stop();
        received = new(TaskCreationOptions.RunContinuationsAsynchronously); watcher.Start(Fixture.Area); var next = await received.Task.WaitAsync(TimeSpan.FromSeconds(5)); Assert.That(next.id, Is.Not.EqualTo(old.id)); Assert.That(watcher.IsCurrent(old.id, old.revision), Is.False);
    }
    [Test]
    public async Task ManualPriorityAndFocusPauseAvoidScreenshots()
    {
        var f = new Fixture { Foreground = false }; using var watcher = f.Watcher(); watcher.Start(Fixture.Area); await Task.Delay(6); Assert.That(f.Grabs, Is.Zero); f.Foreground = true; f.Capture.ManualPending = true; await Task.Delay(6); Assert.That(f.Grabs, Is.Zero); f.Capture.ManualPending = false; var line = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously); watcher.Line += (_, _, _) => line.TrySetResult(); await line.Task.WaitAsync(TimeSpan.FromSeconds(5)); Assert.That(f.Capture.MaxPending, Is.EqualTo(1));
    }
    [Test]
    public async Task DialogueCompletingAfterAltTabStaysSilentUntilGameResumes()
    {
        var f = new Fixture(); f.Settings.AutoRead.Speed = "instant";
        f.Capture.HeldResult = new(TaskCreationOptions.RunContinuationsAsynchronously);
        using var watcher = f.Watcher(); var spoken = new System.Collections.Concurrent.ConcurrentQueue<CaptureResult>();
        var paused = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var resumedLine = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        watcher.Line += (_, _, result) => { spoken.Enqueue(result); resumedLine.TrySetResult(); };
        watcher.Status += (message, _) => { if (message.Contains("paused")) paused.TrySetResult(); };
        watcher.Start(Fixture.Area); await f.Capture.Entered.Task.WaitAsync(TimeSpan.FromSeconds(5));
        f.Foreground = false; f.Capture.HeldResult.SetResult(FakeCapture.StableResult);
        await paused.Task.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.That(spoken, Is.Empty, "An OCR result captured before Alt-Tab must not begin speech in the background.");
        f.Foreground = true; await resumedLine.Task.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.That(spoken.Count, Is.EqualTo(1), "The same dialogue must remain eligible after returning to the game.");
    }
    [Test]
    public async Task TransientScreenshotErrorsBackOffAndRecover()
    {
        var f = new Fixture { Failures = 2 }; using var watcher = f.Watcher(); var result = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously); watcher.Line += (_, _, _) => result.TrySetResult(); watcher.Start(Fixture.Area); await result.Task.WaitAsync(TimeSpan.FromSeconds(5)); Assert.That(f.Time - 10000, Is.GreaterThanOrEqualTo(6000));
    }
    [Test]
    public void StopReportsThatAutoReadIsOffOnlyWhenItWasRunning()
    {
        var f = new Fixture(); using var watcher = f.Watcher(); var messages = new System.Collections.Concurrent.ConcurrentQueue<string>(); watcher.Status += (message, _) => messages.Enqueue(message);
        watcher.Stop(); Assert.That(messages, Is.Empty, "Stopping an idle watcher must not report a change.");
        watcher.Start(Fixture.Area); Assert.That(watcher.Active, Is.True); watcher.Stop();
        Assert.That(watcher.Active, Is.False); Assert.That(messages, Does.Contain("Auto-Read is off."), "Every caller that stops Auto-Read must let the UI show it as off.");
    }
    [Test]
    public async Task ThreeScreenshotErrorsStopWithExplanation()
    {
        var f = new Fixture { Failures = 3 }; using var watcher = f.Watcher(); var result = new TaskCompletionSource<string>(TaskCreationOptions.RunContinuationsAsynchronously); watcher.Status += (message, error) => { if (error && message.Contains("stopped")) result.TrySetResult(message); }; watcher.Start(Fixture.Area); Assert.That(await result.Task.WaitAsync(TimeSpan.FromSeconds(5)), Does.Contain("repeated errors")); Assert.That(watcher.Active, Is.False);
    }
}
