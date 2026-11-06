using GameTextReader.Core;
using GameTextReader.Windows;
using NUnit.Framework;

namespace GameTextReader.Tests;

public class TimingMapTests
{
    [TestCase(true)]
    [TestCase(false)]
    public void DecorativeSymbolsAreOmittedOnlyFromNativeInputWithSourceOffsetsIntact(bool structured)
    {
        const string source = "🌱 Café, naïve science & evidence.\n\n🔬 Résumé Ω 水.";
        var doc = SpeechText.Prepare(source);
        var input = SpeechText.Synthesis(doc, true, structured: structured);
        Assert.That(doc.SourceText, Is.EqualTo(source)); Assert.That(doc.SpokenText, Does.Contain("🌱").And.Contain("🔬"));
        Assert.That(input.Text, Does.Not.Contain("🌱").And.Not.Contain("🔬").And.Contain("Café").And.Contain("水"));
        var start = input.Text.IndexOf("Café", StringComparison.Ordinal);
        var mapped = SpeechService.MapTimings([new(0, start, start + 4)], input, doc).Single();
        Assert.That(doc.SourceText[doc.Words[mapped.WordIndex].SourceStart..doc.Words[mapped.WordIndex].SourceEnd], Is.EqualTo("Café"));
    }
    [TestCase("I want to live.")]
    [TestCase("Long live the king!")]
    [TestCase("Where do you live?")]
    [TestCase("Let's live here.")]
    [TestCase("We live in a small town.")]
    [TestCase("Live long and prosper.")]
    [TestCase("They will go live with her.")]
    [TestCase("It's where we live from now on.")]
    [TestCase("YOU WILL LIVE.")]
    public void LiveMeaningToExistIsRespelledForTheVoice(string source)
    {
        foreach (var structured in new[] { true, false })
            Assert.That(SpeechText.Synthesis(SpeechText.Prepare(source), true, structured: structured).Text, Does.Match("(?i)\\bliv\\b").And.Not.Match("(?i)\\blive\\b"));
    }

    [TestCase("We are live.")]
    [TestCase("We're now live.")]
    [TestCase("The live show starts soon.")]
    [TestCase("Watch this live stream.")]
    [TestCase("They perform live every week.")]
    [TestCase("We go live soon.")]
    [TestCase("Going live in five minutes.")]
    [TestCase("Live from the arena!")]
    [TestCase("Live on stage tonight.")]
    [TestCase("A live-action movie.")]
    public void LiveMeaningRealTimeIsLeftForTheVoice(string source)
    {
        Assert.That(SpeechText.Synthesis(SpeechText.Prepare(source), true).Text, Is.EqualTo(source));
    }

    [TestCase(false)]
    [TestCase(true)]
    public void RespelledLiveKeepsDocumentAndHighlightOffsets(bool structured)
    {
        foreach (var source in new[] { "They live here.", "Where they LIVE", "Heading\n\n- They live.\n- Next line." })
        {
            var doc = SpeechText.Prepare(source);
            var input = SpeechText.Synthesis(doc, true, structured: structured);
            var word = doc.Words.Single(w => doc.SpokenText[w.SpokenStart..w.SpokenEnd].Equals("live", StringComparison.OrdinalIgnoreCase));
            var start = Array.IndexOf(input.NativeToSpoken, word.SpokenStart);
            while (input.Text[start] == '<') start = input.Text.IndexOf('>', start) + 1;
            Assert.That(input.Text.Substring(start, 3), Is.EqualTo("liv").IgnoreCase);
            Assert.That(input.NativeToSpoken.Length, Is.EqualTo(input.Text.Length + 1));
            var mapped = SpeechService.MapTimings([new(0, start, start + 3)], input, doc).Single();
            Assert.That(doc.SourceText[doc.Words[mapped.WordIndex].SourceStart..doc.Words[mapped.WordIndex].SourceEnd], Is.EqualTo("live").IgnoreCase);
            Assert.That(mapped.End, Is.EqualTo(word.SpokenEnd));
            Assert.That(doc.SpokenText, Does.Contain("live").IgnoreCase);
        }
    }

    [Test]
    public void MalformedNativeBoundariesAreRejectedBeforeIndexing()
    {
        var document = SpeechText.Prepare("First sentence.\n\nSecond sentence.");
        var input = SpeechText.Synthesis(document, false);
        var bad = new List<SpeechService.Timing> {
            new(0, -2, 1), new(0, 0, -2), new(0, 9, 3),
            new(0, int.MaxValue, int.MaxValue), new(double.NaN, 0, 3),
            new(double.PositiveInfinity, 0, 3), new(-1, 0, 3)
        };
        SpeechService.Timing[]? mapped = null;
        Assert.DoesNotThrow(() => mapped = SpeechService.MapTimings(bad, input, document));
        Assert.That(mapped, Is.Empty);
    }

    [Test]
    public void PlainTimingWithInvalidLengthDoesNotBecomeAWord()
    {
        var document = SpeechText.Prepare("Repeated words repeated words.");
        var input = SpeechText.Synthesis(document, true, structured: false);
        var mapped = SpeechService.MapTimings([new(0, 0, -2), new(0, -3, 2), new(0, 0, int.MaxValue)], input, document);
        Assert.That(mapped, Is.Empty);
    }

    [Test]
    public void NativePacketValidationRejectsWrongStreamOverflowAndOutOfAudioTimes()
    {
        Assert.That(NativeSapiVoice.TryWord(2, 1, 0, 0, 3, 20, 44100, 88200, out _), Is.False);
        Assert.That(NativeSapiVoice.TryWord(1, 1, 0, -1, 3, 20, 44100, 88200, out _), Is.False);
        Assert.That(NativeSapiVoice.TryWord(1, 1, 0, 0, ulong.MaxValue, 20, 44100, 88200, out _), Is.False);
        Assert.That(NativeSapiVoice.TryWord(1, 1, ulong.MaxValue, 0, 3, 20, 44100, 88200, out _), Is.False);
        Assert.That(NativeSapiVoice.TryWord(1, 1, 44100, 2, 3, 20, 44100, 88200, out var word), Is.True);
        Assert.That(word, Is.EqualTo(new NativeSapiVoice.Word(1, 2, 5)));
    }

    [TestCase(false)]
    [TestCase(true)]
    public void ExactInputOffsetsKeepRepeatedWordsAlignedAcrossMarkupAndUtf16(bool sapi)
    {
        var doc = SpeechText.Prepare("# 🌱 Evidence\n\n1. **Observation & Questioning:** Repeated words repeated words.\n2. **Results:** Keep <XML> safe.", true);
        var input = SpeechText.Synthesis(doc, sapi);
        var native = new List<SpeechService.Timing>();
        foreach (var word in doc.Words)
        {
            var start = Array.IndexOf(input.NativeToSpoken, word.SpokenStart);
            while (start < input.Text.Length && input.Text[start] == '<') start = input.Text.IndexOf('>', start) + 1;
            var end = Array.IndexOf(input.NativeToSpoken, word.SpokenEnd, start);
            native.Add(new(native.Count / 2d, start, end));
        }
        var mapped = SpeechService.MapTimings(native, input, doc);
        Assert.That(mapped.Select(t => t.WordIndex), Is.EqualTo(Enumerable.Range(0, doc.Words.Count)));
        Assert.That(mapped.Select(t => (t.Start, t.End)), Is.EqualTo(doc.Words.Select(w => (w.SpokenStart, w.SpokenEnd))));
    }
}
