using System.Diagnostics;
using System.IO;
using System.Text.Json;
using GameTextReader.Core;
using GameTextReader.Windows;
using NUnit.Framework;

namespace GameTextReader.Tests;

public class CoreTests
{
    private static readonly Lazy<JsonDocument> Golden = new(() => JsonDocument.Parse(File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "Fixtures", "python-baseline.json"))));
    private static readonly OcrCorrector Corrector = new();
    public static IEnumerable<TestCaseData> SpeechCases() => Golden.Value.RootElement.GetProperty("speech").EnumerateArray().Select((value, i) => new TestCaseData(value).SetName("PythonSpeechParity_" + i));
    public static IEnumerable<TestCaseData> CorrectionCases() => Golden.Value.RootElement.GetProperty("corrections").EnumerateArray().Select((value, i) => new TestCaseData(value).SetName("PythonCorrectionParity_" + i));
    [TestCaseSource(nameof(SpeechCases))]
    public void SpeechParity(JsonElement example)
    {
        var source = example.GetProperty("source").GetString()!; var markdown = example.GetProperty("markdown").GetBoolean();
        // Python's Markdown-only path missed Unicode bullets; the documented list behavior is the plain fixture.
        if (markdown && source.Contains('•')) example = Golden.Value.RootElement.GetProperty("speech").EnumerateArray().First(e => e.GetProperty("source").GetString() == source && !e.GetProperty("markdown").GetBoolean());
        var d = SpeechText.Prepare(source, markdown);
        Assert.That(d.SpokenText, Is.EqualTo(example.GetProperty("spoken").GetString()));
        var expectedWords = example.GetProperty("words").EnumerateArray().Select(w => new SpeechWord(w.GetProperty("spoken_start").GetInt32(), w.GetProperty("spoken_end").GetInt32(), w.GetProperty("source_start").GetInt32(), w.GetProperty("source_end").GetInt32())).ToArray();
        Assert.That(d.Words, Is.EqualTo(expectedWords));
        var expectedPauses = example.GetProperty("pauses").EnumerateArray().Select(p => new SpeechPause(p.GetProperty("spoken_offset").GetInt32(), p.GetProperty("source_offset").GetInt32(), p.GetProperty("milliseconds").GetInt32())).ToArray(); Assert.That(d.Pauses, Is.EqualTo(expectedPauses));
    }
    [TestCaseSource(nameof(CorrectionCases))]
    public void CorrectionParity(JsonElement example)
    {
        var result = Corrector.Correct(example.GetProperty("raw").GetString()!, new() { Strength = example.GetProperty("strength").GetString()! });
        Assert.That(result.CorrectedText, Is.EqualTo(example.GetProperty("corrected").GetString()));
        var expected = example.GetProperty("suggestions").EnumerateArray().Select(s => s.GetProperty("original").GetString() + ":" + string.Join('|', s.GetProperty("alternatives").EnumerateArray().Select(a => a.GetString())));
        Assert.That(result.Suggestions.Select(s => s.Original + ":" + string.Join('|', s.Alternatives)), Is.EqualTo(expected));
    }
    [Test]
    public void SettingsDefaultsAndInvalidValues()
    {
        var s = SettingsCodec.Read("{\"theme\":\" DARK \",\"volume\":200,\"speech\":{\"capture_mode\":\"overlap\",\"reader_playback_rate\":\"bad\"},\"auto_read\":{\"speed\":\"instant\"}}");
        Assert.Multiple(() => { Assert.That(s.Theme, Is.EqualTo("dark")); Assert.That(s.Volume, Is.EqualTo(100)); Assert.That(s.Speech.CaptureMode, Is.EqualTo("overlap")); Assert.That(s.Speech.ReaderPlaybackRate, Is.EqualTo(1)); Assert.That(s.AutoRead.Speed, Is.EqualTo("instant")); Assert.That(s.Hotkeys.Snippet, Is.EqualTo("Alt+S")); });
    }
    [Test]
    public void MalformedRuleDoesNotDiscardValidSection()
    {
        var s = SettingsCodec.Read("{\"ocr\":{\"strength\":\"strong\",\"replacements\":[{\"original\":\"A\",\"replacement\":\"B\"},{\"original\":123,\"replacement\":\"x\"},{\"original\":\"C\",\"replacement\":\"D\"}]},\"capture_profiles\":[{\"id\":\"one\",\"name\":\"First\"},123,{\"id\":\"two\",\"name\":\"Second\"}]}");
        Assert.That(s.Ocr.Strength, Is.EqualTo("strong")); Assert.That(s.Ocr.Replacements.Count, Is.EqualTo(2)); Assert.That(s.Profiles.Count, Is.EqualTo(2));
    }
    [Test] public void FractionalReaderRateAndVolumeSurviveRoundTrip() { var s = SettingsCodec.Read("{\"volume\":73.54,\"speech\":{\"reader_playback_rate\":0.9}}"); var restored = SettingsCodec.Read(SettingsCodec.Write(s)); Assert.That(restored.Volume, Is.EqualTo(73.54)); Assert.That(restored.Speech.ReaderPlaybackRate, Is.EqualTo(.9)); }
    [Test]
    public void InvalidProfileIdentityAndPositionDoNotDiscardValidSettings()
    {
        var s = SettingsCodec.Read("""{"window":{"width":1400,"height":900,"x":"bad","y":-120.5},"capture_profiles":[{"name":"Broken"},{"id":123},{"id":"default","name":"Valid","capture_area":{"box":[1,2,30,40]}}]}""");
        Assert.That(s.Profiles.Count, Is.EqualTo(1)); Assert.That(s.SelectedProfile.Name, Is.EqualTo("Valid")); Assert.That(s.FixedBox, Is.EqualTo(new[] { 1, 2, 30, 40 })); Assert.That(s.Window.Width, Is.EqualTo(1400)); Assert.That(s.Window.X, Is.Null); Assert.That(s.Window.Y, Is.EqualTo(-120));
    }
    [Test]
    public void InvalidMonitorMetadataKeepsAbsoluteCaptureBox()
    {
        var s = SettingsCodec.Read("""{"capture_profiles":[{"id":"default","capture_area":{"box":[1,2,300,100],"monitor_bounds":"broken","monitor_dpi":{},"relative_box":[0,"bad",1,1]}}]}""");
        Assert.That(s.FixedBox, Is.EqualTo(new[] { 1, 2, 300, 100 })); Assert.That(s.SelectedProfile.Area!.RelativeBox, Is.Null);
    }
    private sealed class FakeStartup : IStartupStore
    {
        public string? Command; public bool Denied;
        public string? Read() => Command;
        public void Write(string? command) { if (Denied) throw new UnauthorizedAccessException("policy denied"); Command = command; }
    }
    [Test]
    public void StartupRegistrationEnableDisableRepairAndFailureUseIsolatedStore()
    {
        var store = new FakeStartup(); var registration = new StartupRegistration(store); Assert.That(registration.IsEnabled(), Is.False);
        registration.SetEnabled(true, @"C:\Games\Old Folder\GameTextReader.exe"); Assert.That(registration.IsEnabled(), Is.True);
        registration.SetEnabled(true, @"C:\Games\New Folder\GameTextReader.exe"); Assert.That(store.Command, Is.EqualTo("\"C:\\Games\\New Folder\\GameTextReader.exe\" --startup"));
        var previous = store.Command; store.Denied = true; Assert.Throws<UnauthorizedAccessException>(() => registration.SetEnabled(false, "ignored.exe")); Assert.That(store.Command, Is.EqualTo(previous));
        store.Denied = false; registration.SetEnabled(false, "ignored.exe"); Assert.That(registration.IsEnabled(), Is.False);
    }
    [Test]
    public void ImportedSettingsSurviveStaleUiSaveOnClosing()
    {
        var folder = Path.Combine(Path.GetTempPath(), "gtr-import-test-" + Guid.NewGuid()); Directory.CreateDirectory(folder); var old = Path.Combine(folder, "old"); Directory.CreateDirectory(old);
        try
        {
            var path = Path.Combine(folder, "config.json"); var store = new ConfigStore(path); var stale = store.Load(); store.Save(stale);
            File.WriteAllText(Path.Combine(old, "config.json"), """{"theme":"light","hotkeys":{"snippet":"Ctrl+Shift+T"}}"""); store.Import(old);
            Assert.Throws<InvalidOperationException>(() => store.Save(stale)); var imported = new ConfigStore(path).Load(); Assert.That(imported.Theme, Is.EqualTo("light")); Assert.That(imported.Hotkeys.Snippet, Is.EqualTo("Ctrl+Shift+T")); Assert.That(File.Exists(Path.Combine(folder, "config.before-import.json")), Is.True);
        }
        finally { Directory.Delete(folder, true); }
    }
    [Test]
    public void ConfigBackupAndPortableRoundTrip()
    {
        var folder = Path.Combine(Path.GetTempPath(), "gtr-csharp-test-" + Guid.NewGuid()); Directory.CreateDirectory(folder);
        try { var path = Path.Combine(folder, "config.json"); File.WriteAllText(path, "{\"theme\":\"dark\",\"fixed_box\":[1,2,301,102],\"hotkeys\":{\"snippet\":\"Ctrl+Shift+T\"}}"); var store = new ConfigStore(path); var s = store.Load(); s.AutoRead.Speed = "fast"; store.Save(s); Assert.That(File.Exists(Path.Combine(folder, "config.before-csharp.json")), Is.True); var updated = new ConfigStore(path).Load(); Assert.That(updated.AutoRead.Speed, Is.EqualTo("fast")); Assert.That(updated.FixedBox, Is.EqualTo(new[] { 1, 2, 301, 102 })); Assert.That(updated.Hotkeys.Snippet, Is.EqualTo("Ctrl+Shift+T")); }
        finally { Directory.Delete(folder, true); }
    }
    [Test]
    public void CorruptFileCannotBeOverwrittenByDefaults()
    {
        var folder = Path.Combine(Path.GetTempPath(), "gtr-csharp-test-" + Guid.NewGuid()); Directory.CreateDirectory(folder);
        try { var path = Path.Combine(folder, "config.json"); File.WriteAllText(path, "broken{"); var store = new ConfigStore(path); Assert.Catch<JsonException>(() => store.Load()); Assert.Throws<InvalidDataException>(() => store.Save(new())); Assert.That(File.ReadAllText(path), Is.EqualTo("broken{")); }
        finally { Directory.Delete(folder, true); }
    }
    [Test]
    public void ProfileResolutionUsesIdentityAndPhysicalPixels()
    {
        var old = new MonitorInfo("DISPLAY2", 1920, 0, 3840, 1080); var area = CaptureGeometry.Build([2020, 100, 3020, 300], [old]); var current = new MonitorInfo("display2", 0, -1440, 2560, 0, 144, 144);
        Assert.That(CaptureGeometry.Resolve(area, [current]), Is.EqualTo(new[] { 133, -1307, 1467, -1040 })); Assert.Throws<InvalidOperationException>(() => CaptureGeometry.Resolve(area, [old with { Device = "other" }]));
    }
    [Test]
    public void RulesRemainLiteralCascadingAndRawMapped()
    {
        var result = Corrector.Correct("abcdef", new() { Enabled = false, Replacements = [new() { Original = "abcdef", Replacement = "xy" }, new() { Original = "xy", Replacement = "$1\\z" }] }); Assert.That(result.CorrectedText, Is.EqualTo("$1\\z")); Assert.That(result.Corrections.All(c => c.Start == 0 && c.End == 6), Is.True);
    }
    [Test]
    public void ProtectedOutputsAndTermsSurviveAllStrengths()
    {
        var result = Corrector.Correct("Xion Yohan tbe X.", new() { Strength = "strong", ProtectedWords = ["tbe"], Replacements = [new() { Original = "X", Replacement = "warrlor" }] }); Assert.That(result.CorrectedText, Is.EqualTo("Xion Yohan tbe warrlor."));
    }
    [Test]
    public void ReviewUpdatesOneOccurrenceAndRejectsStaleChoice()
    {
        var r = Corrector.Correct("Machine ports. Machine ports.", new()); var first = r.Suggestions[0]; var reviewed = r.Review(first, "parts"); Assert.That(reviewed.CorrectedText, Is.EqualTo("Machine parts. Machine ports.")); Assert.That(reviewed.RawText, Is.EqualTo(r.RawText)); Assert.Throws<InvalidOperationException>(() => reviewed.Review(first, "parts")); Assert.That(reviewed.Suggestions[0].Start, Is.EqualTo(r.Suggestions[1].Start));
    }
    [TestCase("normal", 2)]
    [TestCase("fast", 2)]
    [TestCase("instant", 1)]
    public void AutoReadConfirmationAndBlankRearm(string speed, int count)
    {
        var gate = new DialogueGate(); var policy = AutoReadPolicy.For(speed); for (int i = 1; i < count; i++) Assert.That(gate.Observe("Hello there.", policy, i * 100), Is.EqualTo(DialogueDecision.Ignore)); Assert.That(gate.Observe("Hello there.", policy, 200), Is.EqualTo(DialogueDecision.Speak)); Assert.That(gate.Observe("Hello there.", policy, 300), Is.EqualTo(DialogueDecision.Ignore)); gate.Observe("", policy, 400); gate.Observe("", policy, 500); for (int i = 1; i <= count; i++) gate.Observe("Hello there.", policy, 500 + i * 100); Assert.That(gate.LastSpoken, Is.EqualTo("hello there."));
    }
    [Test]
    public void InstantGrowthCancelsOnceAndWaitsForStableCompleteText()
    {
        var gate = new DialogueGate(); var p = AutoReadPolicy.For("instant"); Assert.That(gate.Observe("I saw", p, 0), Is.EqualTo(DialogueDecision.Speak)); Assert.That(gate.Observe("I saw an", p, 100), Is.EqualTo(DialogueDecision.CancelGrowth)); Assert.That(gate.Observe("I saw an Angel.", p, 150), Is.EqualTo(DialogueDecision.Ignore)); Assert.That(gate.Observe("I saw an Angel.", p, 300), Is.EqualTo(DialogueDecision.Ignore)); Assert.That(gate.Observe("I saw an Angel.", p, 410), Is.EqualTo(DialogueDecision.Speak));
    }
    [Test] public void SimilarDialogueRequiresConfirmation() { Assert.That(DialogueGate.Similar("I like apples today.", "I like red apples today."), Is.True); }
    [Test]
    public void MarkdownIsSanitizedMappedAndImagesStayInert()
    {
        var text = "# Title\n\n1. **Observation:** Repeat repeat.\n\n<script>alert('x')</script><img src='https://evil/a'>\n\n![Diagram](https://example.com/a.png)\n\n[Bad](javascript:alert(1))"; var d = SpeechText.Prepare(text, true); var preview = MarkdownService.Render(text, d);
        Assert.That(preview.Html, Does.Not.Contain("<script").And.Not.Contain("<img").And.Not.Contain("javascript:")); Assert.That(preview.Images, Is.EqualTo(new[] { "https://example.com/a.png" })); Assert.That(preview.Html, Does.Contain("data-reader-word-index")); Assert.That(d.SpokenText, Does.Contain("one: Observation"));
    }
    [Test] public void SourceMappedSuffixRetainsPausesAndFullSource() { var d = SpeechText.Prepare("# Title\n\n- **Label:** explanation\n- next", true); var offset = d.SourceText.IndexOf("explanation", StringComparison.Ordinal); var suffix = d.FromSource(offset); Assert.That(suffix.SourceText, Is.EqualTo(d.SourceText)); Assert.That(suffix.Words[0].SourceStart, Is.EqualTo(offset)); Assert.That(suffix.Pauses.All(p => p.SpokenOffset > 0), Is.True); }
    [Test] public void OrderedMarkersAndExpandableHtmlAreSourceMapped() { var source = "1. **Observe:** repeat\n2. **Question:** repeat\n\n<details><summary>More</summary>Extra prose.</details>"; var d = SpeechText.Prepare(source, true); var html = MarkdownService.Render(source, d).Html; Assert.That(html, Does.Contain("ordered-marker").And.Contain(">1.</span>").And.Contain(">2.</span>")); var extra = d.Words.ToList().FindIndex(w => w.SourceStart == source.IndexOf("Extra", StringComparison.Ordinal)); Assert.That(html, Does.Contain($"data-reader-word-index=\"{extra}\">Extra</span>")); }
    [Test]
    public void CompleteSuppliedMarkdownSampleRendersAndSpeaksProse()
    {
        var source = File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "Fixtures", "science-sample.md")); var d = SpeechText.Prepare(source, true); var preview = MarkdownService.Render(source, d);
        foreach (var token in new[] { "<table>", "<details>", "math-inline", "math-block", "<pre>", "footnote" }) Assert.That(preview.Html.Contains(token), Is.True, "Missing rendered construct: " + token); Assert.That(preview.Images.Count, Is.EqualTo(1)); Assert.That(preview.Html, Does.Not.Contain("<img"));
        Assert.That(d.SpokenText, Does.Contain("Observation & Questioning").And.Contain("Sagan, Carl").And.Contain("Hypothesis vs. Law vs. Theory").And.Not.Contain("import numpy").And.Not.Contain("frac").And.Not.Contain("https://")); Assert.That(d.Pauses.Any(p => p.Milliseconds == 700), Is.True); Assert.That(d.Words.All(w => w.SourceStart >= 0 && w.SourceEnd <= source.Length && w.SpokenEnd <= d.SpokenText.Length), Is.True);
    }
    [Test] public void XmlEscapingKeepsNativeOffsets() { var d = SpeechText.Prepare("Heading\n\nA & B < C 😀."); var input = SpeechText.Synthesis(d, false); Assert.That(input.Text, Does.Contain("&amp;").And.Contain("&lt;")); Assert.That(input.NativeToSpoken.Length, Is.EqualTo(input.Text.Length + 1)); Assert.That(input.NativeToSpoken[^1], Is.EqualTo(d.SpokenText.Length)); }
    [TestCase("Alt+S")]
    [TestCase("Ctrl+Shift+T")]
    [TestCase("Alt+X")]
    [TestCase("Shift+Win+F8")]
    [TestCase("Ctrl+Shift+Space")]
    public void ShortcutParsing(string key) => Assert.That(HotkeyParser.Parse(key)!.Display, Is.EqualTo(key));
    [TestCase("F12")]
    [TestCase("A")]
    [TestCase("Ctrl+Ctrl+T")]
    [TestCase("Ctrl+Shift")]
    public void InvalidShortcutRejected(string key) => Assert.Throws<ArgumentException>(() => HotkeyParser.Parse(key));
    [Test] public void StartupCommandQuotesSpaces() => Assert.That(StartupRegistration.Command(@"C:\Some Folder\GameTextReader.exe"), Is.EqualTo("\"C:\\Some Folder\\GameTextReader.exe\" --startup"));
    [Test] public void OcrLayoutPreservesTitleGap() => Assert.That(OcrService.Layout([new("Title", 0, 0, 100, 20), new("Body", 0, 70, 100, 20), new("wrapped", 0, 95, 100, 20)]), Is.EqualTo("Title\n\nBody\nwrapped"));
    [Test] public void WarmCorrectionPerformance() { Corrector.Preload(); var text = string.Join(' ', Enumerable.Repeat("I don't hove to go bock. Yohan and Xion waited.", 40)); var watch = Stopwatch.StartNew(); Corrector.Correct(text, new() { Strength = "balanced" }); TestContext.Out.WriteLine($"500-word correction: {watch.Elapsed.TotalMilliseconds:0.0} ms"); Assert.That(watch.Elapsed.TotalMilliseconds, Is.LessThan(100)); }
}
