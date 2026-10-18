using System.Diagnostics;
using System.IO;
using System.Text.Json;
using GameTextReader.Core;
using NUnit.Framework;

namespace GameTextReader.Tests;

[NonParallelizable]
public class SpeechProcessTests
{
    [Test]
    public async Task PasteAndPlaySurvivesNativeWordEventsInIsolatedProcess()
    {
        var folder = Path.Combine(Path.GetTempPath(), "gtr-speech-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(folder);
        try
        {
            // Read the preferred voice ID only; never write the normal settings.
            var saved = File.Exists(ConfigStore.DefaultPath) ? SettingsCodec.Read(File.ReadAllText(ConfigStore.DefaultPath)).Voice : "";
            var config = new AppSettings { Voice = saved, Rate = 3, Volume = 0, Theme = "dark" };
            var settings = Path.Combine(folder, "isolated.json");
            File.WriteAllText(settings, SettingsCodec.Write(config));
            var input = Path.Combine(folder, "input.json");
            File.WriteAllText(input, JsonSerializer.Serialize(new { cycles = 2, cases = new[] {
                new { text = "Observation & Questioning.\n\nWe ask why plants grow toward light. More evidence follows.", markdown = false },
                new { text = "# Science & evidence\n\n1. **Observation:** Notice what changes.\n2. **Analysis & Conclusion:** Check results.\n\n- **Self-Correction:** Science is never finished.\n- **Objectivity:** Keep evidence clear.", markdown = true },
                new { text = "Literal XML: <plant> & \"leaf\".\n\nUnicode 🌱 matters. Repeated words repeated words.", markdown = false },
                new { text = File.ReadAllText(Path.Combine(TestContext.CurrentContext.TestDirectory, "Fixtures", "science-sample.md")), markdown = true }
            }}));
            var report = Path.Combine(folder, "report.json");
            var start = new ProcessStartInfo(Path.Combine(TestContext.CurrentContext.TestDirectory, "GameTextReader.exe")) { UseShellExecute = false, CreateNoWindow = true };
            start.Environment["DOTNET_ROOT"] = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".dotnet");
            foreach (var arg in new[] { "--settings", settings, "--speech-probe", input, "--probe-report", report }) start.ArgumentList.Add(arg);
            using var process = Process.Start(start)!;
            using var timeout = new CancellationTokenSource(TimeSpan.FromMinutes(3));
            try { await process.WaitForExitAsync(timeout.Token); }
            catch (OperationCanceledException) { process.Kill(true); Assert.Fail("The isolated paste/Play process stopped responding."); }
            var output = File.Exists(report) ? File.ReadAllText(report) : "The child exited before reporting; inspect Windows Application/.NET Runtime events.";
            Assert.That(process.ExitCode, Is.Zero, "The isolated paste/Play path failed. " + output);
            Assert.That(File.Exists(report), Is.True, "The child exited before reporting completion.");
            using var result = JsonDocument.Parse(File.ReadAllText(report));
            Assert.That(result.RootElement.GetProperty("passed").GetBoolean(), Is.True, result.RootElement.ToString());
            foreach (var sample in result.RootElement.GetProperty("samples").EnumerateArray()) TestContext.Out.WriteLine($"Paste/Play {sample.GetProperty("characters")} characters, cycle {sample.GetProperty("cycle")}: ready {sample.GetProperty("readyMs")} ms; cached={sample.GetProperty("Cached")}");
            var artifactFolder = Path.Combine(TestContext.CurrentContext.TestDirectory, "TestResults"); Directory.CreateDirectory(artifactFolder);
            var artifact = Path.Combine(artifactFolder, "speech-probe-latest.json"); File.Copy(report, artifact, true); TestContext.AddTestAttachment(artifact, "Muted paste/Play stress and native word metadata");
        }
        finally
        {
            // Only this test-created, explicitly resolved temporary directory.
            for (int attempt = 0; attempt < 30; attempt++)
                try { Directory.Delete(folder, true); break; }
                catch (IOException) when (attempt < 29) { await Task.Delay(100); }
        }
    }
}
