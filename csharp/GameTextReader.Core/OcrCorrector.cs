using System.Diagnostics;
using System.Text.RegularExpressions;

namespace GameTextReader.Core;

public sealed record TextCorrection(int Start, int End, string Original, string Replacement, string Reason, double Confidence, string Source = "automatic");
public sealed record CorrectionSuggestion(int Start, int End, string Original, IReadOnlyList<string> Alternatives, string Reason, int RawStart, int RawEnd);
public sealed record CorrectionResult(string RawText, string CorrectedText, IReadOnlyList<TextCorrection> Corrections, double ElapsedMs, IReadOnlyList<CorrectionSuggestion> Suggestions)
{
    public CorrectionResult Review(CorrectionSuggestion suggestion, string? alternative)
    {
        if (!Suggestions.Contains(suggestion) || suggestion.End > CorrectedText.Length || CorrectedText[suggestion.Start..suggestion.End] != suggestion.Original) throw new InvalidOperationException("This suggestion is no longer current.");
        if (alternative == null) return this with { Suggestions = Suggestions.Where(s => s != suggestion).ToArray() };
        if (!suggestion.Alternatives.Contains(alternative)) throw new ArgumentException("Choose a supported alternative.");
        var delta = alternative.Length - suggestion.Original.Length;
        return this with { CorrectedText = CorrectedText[..suggestion.Start] + alternative + CorrectedText[suggestion.End..], Corrections = Corrections.Append(new(suggestion.RawStart, suggestion.RawEnd, suggestion.Original, alternative, suggestion.Reason, 1, "review")).ToArray(), Suggestions = Suggestions.Where(s => s != suggestion && (s.End <= suggestion.Start || s.Start >= suggestion.End)).Select(s => s.Start >= suggestion.End ? s with { Start = s.Start + delta, End = s.End + delta } : s).ToArray() };
    }
}
public sealed class OcrCorrector
{
    private sealed record Candidate(int Start, int End, string Replacement, string Reason, double Confidence = .98, bool Automatic = true, string Source = "automatic");
    private readonly Lazy<(SymSpell engine, Dictionary<string, long> words, Dictionary<string, long> bigrams)> dictionaries;
    public OcrCorrector(string? dictionaryFolder = null) => dictionaries = new(() => Load(dictionaryFolder ?? Path.Combine(AppContext.BaseDirectory, "Assets", "Dictionaries")));
    public void Preload() => _ = dictionaries.Value;
    private static (SymSpell, Dictionary<string, long>, Dictionary<string, long>) Load(string folder)
    {
        var words = new Dictionary<string, long>(); var bigrams = new Dictionary<string, long>();
        var wordPath = Path.Combine(folder, "frequency_dictionary_en_82_765.txt"); var bigramPath = Path.Combine(folder, "frequency_bigramdictionary_en_243_342.txt");
        var engine = new SymSpell(82765, 2, 7);
        if (!engine.LoadDictionary(wordPath, 0, 1)) throw new IOException("The bundled OCR dictionary is missing.");
        engine.LoadBigramDictionary(bigramPath, 0, 2);
        foreach (var line in File.ReadLines(wordPath)) { var cut = line.LastIndexOf(' '); if (cut > 0 && long.TryParse(line[(cut + 1)..], out var n)) words[line[..cut]] = n; }
        foreach (var line in File.ReadLines(bigramPath)) { var cut = line.LastIndexOf(' '); if (cut > 0 && long.TryParse(line[(cut + 1)..], out var n)) bigrams[line[..cut]] = n; }
        return (engine, words, bigrams);
    }
    private static readonly (string pattern, string replacement)[] Grammar =
    [
        (@"\bI (?:definitely\s+|clearly\s+|just\s+|actually\s+|really\s+)?(?<t>sow)(?=\s+(?:it|that|this|him|her|them)\b)", "saw"),
        (@"\bThere (?:was|is) (?<t>o)(?=\s+[A-Za-z])", "a"),
        (@"\bIn (?<t>o)(?=\s+(?:second|moment|minute|hour|day|week|while|flash|heartbeat)\b)", "a"),
        (@"\b(?<t>oll)(?=\s+of\b)", "all"),
        (@"\b(?<t>[l1])(?=\s+(?:am|was|will|would|can|could|have|had|do|did|saw|think|know|need|want|remember|believe)\b)", "I"),
        (@"\b(?<t>5he)(?=\s+(?:is|was|will|would|can|could|has|had|said|went|did|does|looks|seems)\b)", "She"),
        (@"\b(?<t>tbe)\b", "the"), (@"\b(?<t>tbis)\b", "this"), (@"\b(?<t>witb)\b", "with"),
        (@"\b(?:I|you|we|they|I'll|you'll|we'll|they'll|don't|doesn't|didn't|must|will|would|could|should)\s+(?<t>hove)\b", "have"),
        (@"\b(?:go|going|be|come|coming|get|head|went)\s+(?<t>bock)\b", "back"),
        (@"\b(?<t>moke)(?=\s+(?:it|sure|a|an|the|this|that|me|you)\b)", "make"),
        (@"\b(?:he|she|it|land|world|town|place)\s+(?<t>hos)\b", "has"),
        (@"\b(?:take|become|not|was|is|in|for|and|there's|there’s|lot)\s+(?<t>o)(?=\s+\w)", "a"),
        (@"\b(?:I|you|he|she|we|they)\s+(?<t>foiled)(?=,\s*but\b[^!?]{0,100}\bsucceed\b)", "failed"),
        (@"\b(?:I'm|I’m|I am|you're|you are|she is|he is)\s+(?:so\s+|very\s+)?(?<t>scored)(?=[,!.?])", "scared"),
        (@"\b(?:you|I|we|they)\s+(?<t>soy)(?=\s+(?:that|this|it|so|something|anything)\b)", "say"),
        (@"\bkick\s+(?:your|his|her|their|my)\s+(?<t>oss)\b", "ass"),
        (@"\b(?<t>ond)(?=\s+(?:much|more|then|also)\b)", "and"),
        (@"\b(?<t>wont)(?=\s+to\s+(?:go|be|see|know|live|leave|stay)\b)", "want"),
        (@"\b(?<t>l)(?=\s*(?:\.{3}|…))", "I"),
        (@"\b(?<t>1)(?=\s*(?:\.{3}|…)\s*(?:want|wont|wish|hope)\b)", "I"),
        (@"\b(?:that's|that’s|that is|it's|it is|find|found|sent)\s+(?<t>on)(?=\s+(?:order|Angel|enemy|item|answer|opportunity)\b)", "an"),
        (@"\b(?<t>comp)(?=\s+out\b)", "camp"),
        (@"\b(?:vines|apples|pears|fruit),?\s+(?<t>gropes)\b", "grapes"),
        (@"\b(?:painful|harsh|cruel)\s+(?<t>realit)\b", "reality"),
        (@"\b(?<t>dont)(?=\s+(?:have|give|know|want|leave|blame|talk)\b)", "don't"),
        (@"\b(?<t>doesnt)(?=\s+(?:have|know|want|need)\b)", "doesn't")
    ];
    private static readonly (string before, string after)[] Confusions = [("o", "a"), ("a", "o"), ("l", "i"), ("i", "l"), ("rn", "m"), ("m", "rn"), ("cl", "d"), ("d", "cl"), ("c", "e"), ("e", "c"), ("vv", "w"), ("w", "vv"), ("h", "b"), ("b", "h")];
    private static string Case(string original, string replacement) => replacement == "I" ? "I" : original.All(c => !char.IsLetter(c) || char.IsUpper(c)) ? replacement.ToUpperInvariant() : char.IsUpper(original[0]) ? char.ToUpperInvariant(replacement[0]) + replacement[1..] : replacement;
    private static bool Overlap(int a, int b, int c, int d) => a < d && b > c;
    private static List<(int a, int b)> Protected(string text, OcrSettings options)
    {
        var result = new List<(int, int)>();
        foreach (var term in options.ProtectedWords.Concat(options.Replacements.Where(r => r.Enabled).Select(r => r.Replacement)).Where(s => !string.IsNullOrEmpty(s)).Distinct(StringComparer.OrdinalIgnoreCase))
            foreach (Match m in Regex.Matches(text, @"(?<![\w’'])" + Regex.Escape(term) + @"(?![\w’'])", RegexOptions.IgnoreCase)) result.Add((m.Index, m.Index + m.Length));
        return result;
    }
    public CorrectionResult Correct(string raw, OcrSettings options)
    {
        var watch = Stopwatch.StartNew(); var text = raw; var origins = Enumerable.Range(0, raw.Length).Select(i => (start: i, end: i + 1)).ToList(); var changes = new List<TextCorrection>();
        void Apply(IEnumerable<Candidate> candidates)
        {
            var accepted = new List<Candidate>();
            foreach (var c in candidates.OrderBy(c => c.Start)) if (c.Start >= 0 && c.End <= text.Length && !accepted.Any(p => Overlap(c.Start, c.End, p.Start, p.End))) accepted.Add(c);
            foreach (var c in accepted.AsEnumerable().Reverse())
            {
                int a = c.Start < origins.Count ? origins[c.Start].start : raw.Length, b = c.End > c.Start && c.End - 1 < origins.Count ? origins[c.End - 1].end : a;
                changes.Add(new(a, b, text[c.Start..c.End], c.Replacement, c.Reason, c.Confidence, c.Source));
                text = text[..c.Start] + c.Replacement + text[c.End..]; origins.RemoveRange(c.Start, c.End - c.Start); origins.InsertRange(c.Start, Enumerable.Repeat((a, b), c.Replacement.Length));
            }
        }
        foreach (var rule in options.Replacements.Where(r => r.Enabled && r.Original.Length > 0))
        {
            var pattern = (rule.WholeWord ? @"(?<![\w’'])" : "") + Regex.Escape(rule.Original) + (rule.WholeWord ? @"(?![\w’'])" : "");
            Apply(Regex.Matches(text, pattern, rule.CaseSensitive ? RegexOptions.None : RegexOptions.IgnoreCase).Select(m => new Candidate(m.Index, m.Index + m.Length, rule.Replacement, "custom replacement: " + rule.Original, 1, true, "custom")));
        }
        if (options.Enabled)
        {
            Apply(Analyze(text, options).Where(c => c.Automatic));
            if (options.Strength != "conservative") { Apply(DictionaryRepairs(text, options)); Apply(Spacing(text)); }
        }
        var suggestions = options.Enabled ? Analyze(text, options).Where(c => !c.Automatic).Select(c => new CorrectionSuggestion(c.Start, c.End, text[c.Start..c.End], [c.Replacement], c.Reason, c.Start < origins.Count ? origins[c.Start].start : 0, c.End > 0 && c.End <= origins.Count ? origins[c.End - 1].end : raw.Length)).ToArray() : [];
        return new(raw, text, changes.OrderBy(c => c.Start).ToArray(), watch.Elapsed.TotalMilliseconds, suggestions);
    }
    private List<Candidate> Analyze(string text, OcrSettings options)
    {
        var candidates = new List<Candidate>(); var protectedRanges = Protected(text, options);
        bool Blocked(int a, int b) => protectedRanges.Any(r => Overlap(a, b, r.a, r.b));
        void Add(int a, int b, string replacement, string reason, double confidence = .98, bool automatic = true, bool preserveCase = true)
        {
            if (!Blocked(a, b) && text[a..b] != replacement) candidates.Add(new(a, b, preserveCase ? Case(text[a..b], replacement) : replacement, reason, confidence, automatic));
        }
        foreach (var rule in Grammar) foreach (Match m in Regex.Matches(text, rule.pattern, RegexOptions.IgnoreCase)) { var g = m.Groups["t"]; Add(g.Index, g.Index + g.Length, rule.replacement, "grammar and OCR character-confusion context"); }
        foreach (Match m in Regex.Matches(text, @"\bsent (?<t>on)(?=\s+[A-Z][A-Za-z'’-]*(?:\s+for\b|[.!?,;:]))")) { var g = m.Groups["t"]; Add(g.Index, g.Index + g.Length, "an", "article after sent"); }
        var anchors = Regex.Matches(text, @"\b([A-Z][a-z]{2,})\s*,\s*(?:do you|you|why|please)\b");
        foreach (Match m in Regex.Matches(text, @"\b(?:Thank you|thanks)[,.…\s]+(?<name>[A-Z][a-z]{2,})\b", RegexOptions.IgnoreCase))
        {
            var g = m.Groups["name"]; var possible = Visual(g.Value.ToLowerInvariant()); var names = anchors.Where(a => a.Index < g.Index && possible.Contains(a.Groups[1].Value.ToLowerInvariant())).Select(a => a.Groups[1].Value).Distinct().ToArray();
            if (names.Length == 1 && !anchors.Any(a => a.Groups[1].Value.Equals(g.Value, StringComparison.OrdinalIgnoreCase))) Add(g.Index, g.Index + g.Length, names[0], "earlier address in this passage identifies the name", .97);
        }
        foreach (var (pattern, replacement, reason) in new[] { (@"\b(?:see|visit)\s+(?<t>t\s+t\s+ace)\b", "that place", "several letters are missing; verify the proposed phrase"), (@"\blike\s+(?<t>Ing)(?=\s+the\s+lottery\b)", "winning", "incomplete word; verify against the game"), (@"\bpond\s+in\s+(?:a|o)\s+(?<t>cove)\b", "cave", "both readings are valid words; check the original"), (@"\bmachine\s+(?<t>ports)\b", "parts", "both readings are valid words; check the original") })
            foreach (Match m in Regex.Matches(text, pattern, pattern.Contains("pond") || pattern.Contains("machine") ? RegexOptions.IgnoreCase : RegexOptions.None)) { var g = m.Groups["t"]; Add(g.Index, g.Index + g.Length, replacement, reason, .65, false, false); }
        var dict = dictionaries.Value;
        foreach (Match m in Regex.Matches(text, @"\b([a-z]{2,})[-]?\n[ \t]*([a-z]{2,})\b"))
        {
            var first = m.Groups[1].Value; var second = m.Groups[2].Value; var joined = first + second;
            var before = Regex.Matches(text[..m.Index], @"[A-Za-z]+").LastOrDefault()?.Value.ToLowerInvariant() ?? ""; var after = Regex.Match(text[(m.Index + m.Length)..], @"[A-Za-z]+").Value.ToLowerInvariant();
            var context = dict.bigrams.GetValueOrDefault(before + " " + joined) > 10000 && dict.bigrams.GetValueOrDefault(joined + " " + after) > 10000;
            if (dict.words.GetValueOrDefault(joined) >= 10000 && (!dict.words.ContainsKey(first) || !dict.words.ContainsKey(second))) Add(m.Index, m.Index + m.Length, joined, "line-split word and neighboring phrase evidence", context ? .97 : .7, context && options.Strength != "conservative");
        }
        var tokens = Regex.Matches(text, @"[A-Za-z]+(?:['’][A-Za-z]+)*").ToArray();
        for (int i = 0; i < tokens.Length; i++)
        {
            var m = tokens[i]; var word = m.Value; int a = m.Index, b = a + m.Length;
            if (word.Length is < 3 or > 48 || word.Any(char.IsUpper) || Blocked(a, b) || candidates.Any(c => Overlap(a, b, c.Start, c.End))) continue;
            var before = i > 0 && !Regex.IsMatch(text[(tokens[i - 1].Index + tokens[i - 1].Length)..a], @"[.!?…:;•]|\n\s*\n") ? tokens[i - 1].Value.ToLowerInvariant() : "";
            var after = i + 1 < tokens.Length && !Regex.IsMatch(text[b..tokens[i + 1].Index], @"[.!?…:;•]|\n\s*\n") ? tokens[i + 1].Value.ToLowerInvariant() : "";
            (double score, long left, long right) Evidence(string candidate) { var l = dict.bigrams.GetValueOrDefault(before + " " + candidate); var r = dict.bigrams.GetValueOrDefault(candidate + " " + after); return (Math.Log(1d + l) + Math.Log(1d + r), l, r); }
            var alternatives = Visual(word).Where(c => dict.words.GetValueOrDefault(c) >= 10000).ToHashSet();
            if (options.Strength != "conservative" && !dict.words.ContainsKey(word)) foreach (var c in dict.engine.Lookup(word, SymSpell.Verbosity.Closest, options.Strength == "strong" ? 2 : 1).Take(6)) alternatives.Add(c.term);
            var ranked = alternatives.Select(c => (score: Evidence(c).score - VisualCost(word, c), word: c)).OrderByDescending(c => c.score).ThenByDescending(c => c.word, StringComparer.Ordinal).Take(6).ToArray();
            if (ranked.Length == 0) continue; var best = ranked[0]; var margin = best.score - Math.Max(Evidence(word).score, ranked.Length > 1 ? ranked[1].score : 0); var evidence = Evidence(best.word);
            if (margin < 5 || evidence.left <= 1000 || evidence.right <= 1000 || best.word == word) continue;
            var auto = !dict.words.ContainsKey(word) && options.Strength != "conservative" && evidence.left > 10000 && evidence.right > 10000 && margin >= 8;
            Add(a, b, best.word, "neighboring phrase frequencies and visual similarity", auto ? .94 : .72, auto);
        }
        var accepted = new List<Candidate>(); foreach (var c in candidates.OrderBy(c => !c.Automatic).ThenByDescending(c => c.Confidence).ThenBy(c => c.Start)) if (!accepted.Any(p => Overlap(c.Start, c.End, p.Start, p.End))) accepted.Add(c);
        return accepted.OrderBy(c => c.Start).ToList();
    }
    private static HashSet<string> Visual(string word)
    {
        var candidates = new HashSet<string>(); if (word.Length > 48) return candidates;
        foreach (var (before, after) in Confusions) for (int i = 0; i <= word.Length - before.Length; i++) if (word.AsSpan(i).StartsWith(before)) candidates.Add(word[..i] + after + word[(i + before.Length)..]);
        for (int i = 1; i < word.Length; i++) if (word[i] == word[i - 1]) candidates.Add(word.Remove(i, 1));
        foreach (var c in "abcdefghijklmnopqrstuvwxyz") { candidates.Add(c + word); candidates.Add(word + c); }
        return candidates;
    }
    private static double VisualCost(string word, string candidate)
    {
        foreach (var (before, after) in Confusions) for (int i = 0; i <= word.Length - before.Length; i++) if (word.AsSpan(i).StartsWith(before) && word[..i] + after + word[(i + before.Length)..] == candidate) return (before, after) is ("o", "a") or ("a", "o") or ("l", "i") or ("rn", "m") ? .35 : .6;
        return Math.Abs(word.Length - candidate.Length) <= 1 ? 1.25 : 2;
    }
    private IEnumerable<Candidate> DictionaryRepairs(string text, OcrSettings options)
    {
        var dict = dictionaries.Value; var protectedRanges = Protected(text, options);
        foreach (Match m in Regex.Matches(text, @"(?<![\w’'])(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{4,}(?![\w’'])|(?<![\w’'])[A-Za-z][A-Za-z'’-]{2,}(?![\w’'])"))
        {
            if (protectedRanges.Any(r => Overlap(m.Index, m.Index + m.Length, r.a, r.b))) continue;
            var word = m.Value; var lower = word.ToLowerInvariant(); var digit = word.Count(char.IsDigit) == 1;
            if (!digit && (word.Any(char.IsUpper) || dict.words.ContainsKey(lower))) continue;
            var suggestion = dict.engine.Lookup(lower, SymSpell.Verbosity.Top, options.Strength == "strong" ? 2 : 1, true).FirstOrDefault();
            var before = Regex.Matches(text[Math.Max(0, m.Index - 15)..m.Index].ToLowerInvariant(), "[a-z]+").LastOrDefault()?.Value ?? "";
            if (suggestion != null && suggestion.distance > 0 && suggestion.count >= 10000 && (digit && suggestion.distance == 1 && suggestion.term.All(char.IsLetter) || !digit && dict.bigrams.GetValueOrDefault(before + " " + suggestion.term) > 10000 && new[] { ("l", "i"), ("rn", "m"), ("cl", "d"), ("vv", "w") }.Any(p => { var i = lower.IndexOf(p.Item1, StringComparison.Ordinal); return i >= 0 && lower[..i] + p.Item2 + lower[(i + p.Item1.Length)..] == suggestion.term; })))
            { yield return new(m.Index, m.Index + m.Length, Case(word, suggestion.term), "dictionary-supported OCR character correction", .92); continue; }
            if (word.Length >= 7 && word.All(char.IsLower))
            {
                var segmented = dict.engine.WordSegmentation(lower); var parts = segmented.correctedString.Split(' ');
                if (parts.Length == 2 && parts.All(p => p.Length >= 2) && segmented.distanceSum == 1 && string.Concat(parts) == lower) yield return new(m.Index, m.Index + m.Length, segmented.correctedString, "dictionary-supported missing space", .90);
            }
        }
    }
    private static IEnumerable<Candidate> Spacing(string text)
    {
        foreach (Match m in Regex.Matches(text, @"[ \t]+(?=[,.;:!?])")) yield return new(m.Index, m.Index + m.Length, "", "removed space before punctuation", .99);
        foreach (Match m in Regex.Matches(text, @"(?<=[,;:!?])(?=[A-Za-z])")) yield return new(m.Index, m.Index, " ", "restored space after punctuation", .97);
        foreach (Match m in Regex.Matches(text, @"[ \t]{2,}")) yield return new(m.Index, m.Index + m.Length, " ", "collapsed extra spaces", .99);
    }
}
