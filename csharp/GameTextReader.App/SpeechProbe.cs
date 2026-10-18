using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using GameTextReader.Windows;

namespace GameTextReader.App;

// Runs the actual paste/Play path in a disposable child process. No integrations,
// physical audio, clipboard access, or normal AppData writes are enabled.
public partial class MainWindow
{
    internal async void RunSpeechProbe(string inputPath, string reportPath)
    {
        var samples = new List<object>();
        var errors = new ConcurrentQueue<string>();
        var progress = new ConcurrentQueue<SpeechProgress>();
        var boundaries = new ConcurrentQueue<object>();
        speech.Error += errors.Enqueue;
        speech.Progress += progress.Enqueue;
        speech.BoundaryTrace += (backend, input, timings) => boundaries.Enqueue(new { backend, inputCharacters = input.Text.Length, nativeWords = timings.Count, first = timings.Take(4).Select(t => new { t.Seconds, t.Start, t.End }).ToArray() });
        var result = new Dictionary<string, object?>();
        try
        {
            using var input = JsonDocument.Parse(File.ReadAllText(inputPath));
            settings.Volume = 0;
            var cycleCount = Math.Clamp(input.RootElement.GetProperty("cycles").GetInt32(), 1, 100);
            var caseIndex = 0;
            foreach (var test in input.RootElement.GetProperty("cases").EnumerateArray())
            {
                var source = test.GetProperty("text").GetString()!;
                result["caseIndex"] = caseIndex++;
                for (var cycle = 0; cycle < cycleCount; cycle++)
                {
                    result["cycle"] = cycle;
                    result["characters"] = source.Length;
                    File.WriteAllText(reportPath, JsonSerializer.Serialize(result));
                    SetText(source, test.GetProperty("markdown").GetBoolean());
                    var watch = Stopwatch.StartNew();
                    StartReader(false);
                    var id = currentRequest;
                    SpeechDiagnostic? state = null;
                    while (watch.Elapsed < TimeSpan.FromSeconds(45) && errors.IsEmpty)
                    {
                        state = (await speech.DiagnosticsAsync()).FirstOrDefault(s => s.Id == id);
                        if (state is { Ready: true }) break;
                        await Task.Delay(20);
                    }
                    if (state is not { Ready: true, HasTiming: true }) throw new InvalidOperationException("Paste/Play never obtained a timed audio stream. " + string.Join("; ", errors));
                    samples.Add(new { cycle, characters = source.Length, readyMs = watch.ElapsedMilliseconds, state.Cached });
                    var deadline = Environment.TickCount64 + 4000;
                    while (progress.Count(p => p.RequestId == id) < 3 && Environment.TickCount64 < deadline) await Task.Delay(20);
                    if (progress.Count(p => p.RequestId == id) < 3) throw new InvalidOperationException("Word highlighting did not advance through the passage: " + progress.Count(p => p.RequestId == id) + " events. " + JsonSerializer.Serialize(await speech.DiagnosticsAsync()));
                    speech.Cancel(id);
                    while ((await speech.DiagnosticsAsync()).Any(s => s.Id == id)) await Task.Delay(20);
                }
            }
            if (!errors.IsEmpty) throw new InvalidOperationException(string.Join("; ", errors));
            result["passed"] = true;
        }
        catch (Exception error) { result["passed"] = false; result["error"] = error.ToString(); Environment.ExitCode = 1; }
        finally
        {
            result["samples"] = samples;
            result["boundaries"] = boundaries.ToArray();
            result["progressEvents"] = progress.Count;
            Directory.CreateDirectory(Path.GetDirectoryName(reportPath)!);
            File.WriteAllText(reportPath, JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true }));
            Quit();
        }
    }
}
