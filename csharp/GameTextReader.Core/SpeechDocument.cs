using System.Net;
using System.Text;
using System.Text.RegularExpressions;

namespace GameTextReader.Core;

public sealed record SpeechWord(int SpokenStart, int SpokenEnd, int SourceStart, int SourceEnd);
public sealed record SpeechSentence(int SpokenStart, int SpokenEnd, int SourceStart, int SourceEnd);
public sealed record SpeechPause(int SpokenOffset, int SourceOffset, int Milliseconds);
public sealed record SpeechDocument(string SourceText, string SpokenText, IReadOnlyList<SpeechWord> Words, IReadOnlyList<SpeechSentence> Sentences, IReadOnlyList<SpeechPause> Pauses)
{
    public SpeechDocument FromSource(int offset)
    {
        var word = Words.FirstOrDefault(w => w.SourceEnd > offset);
        if (word == null) return new(SourceText, "", [], [], []);
        var shift = word.SpokenStart; var words = Words.Where(w => w.SpokenStart >= shift).Select(w => w with { SpokenStart = w.SpokenStart - shift, SpokenEnd = w.SpokenEnd - shift }).ToArray();
        return new(SourceText, SpokenText[shift..], words, SpeechText.Sentences(SpokenText[shift..], words), Pauses.Where(p => p.SpokenOffset > shift).Select(p => p with { SpokenOffset = p.SpokenOffset - shift }).ToArray());
    }
}
public sealed record SynthesisInput(string Text, int[] NativeToSpoken, bool Structured);
public static class SpeechText
{
    public static readonly Regex WordPattern = new(@"\w+(?:[\-'’]\w+)*", RegexOptions.Compiled);
    private static readonly Regex Bullet = new(@"^\s*(?:[-+*•◦▪‣⁃∙·●○■□◆◇▶►–—]\s+(?:\[[ xX]\]\s+)?|(?:\d{1,3}|[A-Za-z])[.)]\s+)");
    private static readonly Regex Ordered = new(@"^\s*(\d{1,3})[.)]\s+");
    private static readonly Regex Heading = new(@"^\s{0,3}#{1,6}\s+");
    private static readonly Regex Rule = new(@"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$");
    private static readonly Regex TrailingPause = new("[.,!?…;:][\"'”’\\])}]*$");
    private static readonly HashSet<string> Abbreviations = ["mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc"];
    public static bool LooksLikeMarkdown(string text) => Regex.IsMatch(text, @"(?m)^\s{0,3}(?:#{1,6}\s|>\s|[-+*]\s|\d+[.)]\s|\|.+\|\s*$)|\*\*[^*]+\*\*|\[[^]]+\]\([^)]+\)|```|\$\$|\[\^[^]]+\]:");
    public static string Cardinal(int value)
    {
        string[] ones = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"];
        string[] tens = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"];
        return value < 20 ? ones[value] : value < 100 ? tens[value / 10] + (value % 10 == 0 ? "" : "-" + ones[value % 10]) : ones[value / 100] + " hundred" + (value % 100 == 0 ? "" : " " + Cardinal(value % 100));
    }
    public static SpeechDocument Prepare(string source, bool markdown = false)
    {
        source ??= ""; var output = new StringBuilder(); var origins = new List<int>(); var segment = new List<(char c, int origin)>();
        var masked = markdown ? Regex.Replace(source, @"<(script|style|iframe|object|form)\b[^>]*>.*?</\1\s*>", m => Regex.Replace(m.Value, @"[^\r\n]", " "), RegexOptions.IgnoreCase | RegexOptions.Singleline) : source;
        void Flush()
        {
            if (segment.Count == 0) return;
            var value = new string(segment.Select(x => x.c).ToArray());
            if (string.IsNullOrWhiteSpace(value)) { segment.Clear(); return; }
            if (output.Length > 0) { output.Append(' '); origins.Add(-1); }
            foreach (Match token in Regex.Matches(value, @"\S+"))
            {
                if (output.Length > 0 && output[^1] != ' ' && !Regex.IsMatch(token.Value, @"^[.,!?…;:]+$")) { output.Append(' '); origins.Add(-1); }
                foreach (var pair in segment.Skip(token.Index).Take(token.Length)) { output.Append(pair.c); origins.Add(pair.origin); }
            }
            if (!TrailingPause.IsMatch(output.ToString())) { output.Append('.'); origins.Add(-1); }
            segment.Clear();
        }
        var cursor = 0; bool fence = false, math = false;
        foreach (Match lineMatch in Regex.Matches(masked, @"[^\n]*(?:\n|$)"))
        {
            if (lineMatch.Length == 0) continue;
            var raw = lineMatch.Value; var line = raw.TrimEnd('\r', '\n'); var stripped = line.Trim(); cursor = lineMatch.Index;
            if (markdown && (stripped.StartsWith("```") || stripped.StartsWith("~~~"))) { Flush(); fence = !fence; continue; }
            if (fence) continue;
            if (markdown && stripped.StartsWith("$$")) { Flush(); math = !(math || stripped.EndsWith("$$") && stripped.Length > 2); continue; }
            if (math) continue;
            if (stripped.Length == 0 || Rule.IsMatch(line) || markdown && Regex.IsMatch(line, @"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?$|^\s*\[(?!\^)[^]]+\]:\s+\S+")) { Flush(); continue; }
            var heading = markdown ? Heading.Match(line) : Match.Empty; var bullet = Bullet.Match(line);
            var footnote = markdown ? Regex.Match(line, @"^\s*\[\^[^]]+\]:\s*") : Match.Empty;
            var quote = markdown ? Regex.Match(line, @"^\s*>\s?") : Match.Empty; var table = markdown && line.Count(c => c == '|') >= 2;
            bool block = heading.Success || bullet.Success || footnote.Success || table;
            if (block) Flush(); var prefix = new[] { heading, bullet, footnote, quote }.FirstOrDefault(m => m.Success); var start = prefix?.Length ?? 0;
            if (segment.Count > 0) segment.Add((' ', cursor + start));
            var ordered = bullet.Success ? Ordered.Match(line) : Match.Empty;
            if (ordered.Success)
            {
                var label = Cardinal(int.Parse(ordered.Groups[1].Value)) + ":";
                for (int i = 0; i < label.Length; i++) segment.Add((label[i], cursor + ordered.Groups[1].Index + (i >= label.Length - 2 ? ordered.Groups[1].Length - 1 : 0)));
                segment.Add((' ', cursor + start));
            }
            if (markdown) segment.AddRange(Inline(line[start..], cursor + start));
            else for (int i = start; i < line.Length; i++) segment.Add((line[i], cursor + i));
            if (block) Flush();
        }
        Flush(); var spoken = output.ToString(); var words = new List<SpeechWord>();
        foreach (Match m in WordPattern.Matches(spoken))
        {
            var mapped = origins.Skip(m.Index).Take(m.Length).Where(p => p >= 0).ToArray();
            if (mapped.Length > 0) words.Add(new(m.Index, m.Index + m.Length, mapped[0], mapped[^1] + 1));
        }
        return new(source, spoken, words, Sentences(spoken, words), Pauses(source, words));
    }
    private static IEnumerable<(char c, int origin)> Inline(string line, int offset)
    {
        var result = new List<(char, int)>(); int i = 0;
        while (i < line.Length)
        {
            var part = line[i..];
            if (part.StartsWith("<!--")) { var end = line.IndexOf("-->", i + 4, StringComparison.Ordinal); i = end < 0 ? line.Length : end + 3; continue; }
            if (line[i] == '<') { var end = line.IndexOf('>', i + 1); if (end >= 0) { if (Regex.IsMatch(line[i..(end + 1)], @"^</?(?:details|summary|p|br|div|li|td|th|blockquote)\b", RegexOptions.IgnoreCase) && result.Count > 0 && !char.IsWhiteSpace(result[^1].Item1)) result.Add((' ', offset + i)); i = end + 1; continue; } }
            if (line[i] == '`') { int count = 1; while (i + count < line.Length && line[i + count] == '`') count++; var end = line.IndexOf(new string('`', count), i + count, StringComparison.Ordinal); if (end >= 0) { i = end + count; continue; } }
            if (line[i] == '$') { var end = line.IndexOf('$', i + 1); if (end >= 0) { i = end + 1; continue; } }
            if (line[i] == '&') { var entity = Regex.Match(part, @"^&(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z]+);"); if (entity.Success) { foreach (var c in WebUtility.HtmlDecode(entity.Value)) result.Add((c, offset + i)); i += entity.Length; continue; } }
            if (part.StartsWith("![") || part.StartsWith('['))
            {
                var start = i + (part.StartsWith("![") ? 2 : 1); var end = line.IndexOf(']', start);
                if (end >= 0 && end + 1 < line.Length && (line[end + 1] == '(' || line[end + 1] == '['))
                {
                    var close = line.IndexOf(line[end + 1] == '(' ? ')' : ']', end + 2);
                    if (close >= 0) { result.AddRange(Inline(line[start..end], offset + start)); i = close + 1; continue; }
                }
                if (part.StartsWith("[^") && end >= 0) { i = end + 1; continue; }
            }
            if (line[i] == '\\' && i + 1 < line.Length) i++;
            if (!"*_~|".Contains(line[i])) result.Add((line[i], offset + i)); i++;
        }
        return result;
    }
    public static IReadOnlyList<SpeechSentence> Sentences(string text, IReadOnlyList<SpeechWord> words)
    {
        var result = new List<SpeechSentence>(); var first = 0;
        for (int i = 0; i < words.Count; i++)
        {
            var word = words[i]; var next = i + 1 < words.Count ? words[i + 1] : null; var between = text[word.SpokenEnd..(next?.SpokenStart ?? text.Length)];
            var terminal = Regex.IsMatch(between, "[.!?…]+[\"'”’\\])}]*\\s*$");
            if (next != null && between == "." && int.TryParse(text[word.SpokenStart..word.SpokenEnd], out _) && int.TryParse(text[next.SpokenStart..next.SpokenEnd], out _)) terminal = false;
            if (between.TrimStart().StartsWith('.') && Abbreviations.Contains(text[word.SpokenStart..word.SpokenEnd].ToLowerInvariant())) terminal = false;
            if (next == null || terminal) { var start = words[first]; result.Add(new(start.SpokenStart, word.SpokenEnd, start.SourceStart, word.SourceEnd)); first = i + 1; }
        }
        return result;
    }
    private static IReadOnlyList<SpeechPause> Pauses(string source, IReadOnlyList<SpeechWord> words)
    {
        var result = new Dictionary<int, SpeechPause>(); int pending = 0; int? previousDepth = null;
        void Add(int sourceOffset, int ms)
        {
            var word = words.FirstOrDefault(w => w.SourceStart >= sourceOffset);
            if (word == null || word == words.FirstOrDefault()) return;
            if (!result.TryGetValue(word.SpokenStart, out var old) || ms > old.Milliseconds) result[word.SpokenStart] = new(word.SpokenStart, word.SourceStart, ms);
        }
        foreach (Match raw in Regex.Matches(source, @"[^\n]*(?:\n|$)"))
        {
            if (raw.Length == 0) continue; var line = raw.Value.TrimEnd('\r', '\n');
            if (string.IsNullOrWhiteSpace(line)) { pending = Math.Max(pending, 350); continue; }
            if (Rule.IsMatch(line)) { pending = 700; previousDepth = null; continue; }
            var heading = Heading.IsMatch(line); var item = Bullet.Match(line);
            if (heading) pending = Math.Max(pending, 700);
            else if (item.Success)
            {
                var depth = line.TakeWhile(char.IsWhiteSpace).Sum(c => c == '\t' ? 4 : 1); pending = Math.Max(pending, depth == previousDepth ? 180 : 350); previousDepth = depth;
                var label = Regex.Match(line[item.Length..], @"^(?:\*\*|__|\*|_)?[^:\n.!?]{1,80}:(?:\*\*|__|\*|_)?[ \t]+(?=\S)");
                if (label.Success) Add(raw.Index + item.Length + label.Length, 250);
            }
            else previousDepth = null;
            if (pending > 0) { Add(raw.Index, pending); pending = 0; }
            if (heading) pending = 350;
        }
        return result.Values.OrderBy(p => p.SpokenOffset).ToArray();
    }
    public static SynthesisInput Synthesis(SpeechDocument document, bool sapi, string language = "en-US", bool structured = true)
    {
        var markup = structured && document.Pauses.Count > 0;
        var decorative = document.SpokenText.EnumerateRunes().Any(r => r.Utf16SequenceLength > 1 && Rune.GetUnicodeCategory(r) == System.Globalization.UnicodeCategory.OtherSymbol);
        if (!markup && !decorative) return new(document.SpokenText, Enumerable.Range(0, document.SpokenText.Length + 1).ToArray(), false);
        var text = new StringBuilder(); var map = new List<int>();
        void Emit(string value, int offset) { text.Append(value); map.AddRange(Enumerable.Repeat(offset, value.Length)); }
        if (markup && !sapi) Emit($"<speak version=\"1.0\" xmlns=\"http://www.w3.org/2001/10/synthesis\" xml:lang=\"{WebUtility.HtmlEncode(language)}\">", 0);
        var pauses = document.Pauses.ToDictionary(p => p.SpokenOffset, p => p.Milliseconds);
        for (int i = 0; i < document.SpokenText.Length; i++)
        {
            if (markup && pauses.TryGetValue(i, out var delay)) Emit(sapi ? $"<silence msec=\"{delay}\"/>" : $"<break time=\"{delay}ms\"/>", i);
            if (char.IsSurrogatePair(document.SpokenText, i) && Rune.GetUnicodeCategory(new Rune(char.ConvertToUtf32(document.SpokenText, i))) == System.Globalization.UnicodeCategory.OtherSymbol)
            {
                // These decorative, non-word symbols corrupt word positions in
                // Windows/Aria (even as numeric XML entities). Do not send them
                // to the engine; preserve the authoritative text and offsets.
                Emit(" ", i);
                i++;
            }
            else Emit(markup ? document.SpokenText[i] switch { '&' => "&amp;", '<' => "&lt;", '>' => "&gt;", _ => document.SpokenText[i].ToString() } : document.SpokenText[i].ToString(), i);
        }
        if (markup && !sapi) Emit("</speak>", document.SpokenText.Length); map.Add(document.SpokenText.Length); return new(text.ToString(), map.ToArray(), markup);
    }
}
