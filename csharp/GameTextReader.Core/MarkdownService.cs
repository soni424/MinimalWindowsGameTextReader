using System.Net;
using System.Text.RegularExpressions;
using Ganss.Xss;
using Markdig;
using Markdig.Renderers;
using Markdig.Renderers.Html;
using Markdig.Syntax;
using Markdig.Syntax.Inlines;
using Markdig.Extensions.Mathematics;

namespace GameTextReader.Core;

public sealed record MarkdownPreview(string Html, IReadOnlyList<string> Images);
public static class MarkdownService
{
    private static readonly MarkdownPipeline Pipeline = new MarkdownPipelineBuilder().UsePipeTables().UseTaskLists().UseFootnotes().UseEmphasisExtras().UseMathematics().Build();
    public static MarkdownPreview Render(string source, SpeechDocument speech)
    {
        // Raw image tags do not bypass consent, and removed blocks must not execute or narrate.
        var safeSource = SpeechText.MaskUnsafeBlocks(source);
        safeSource = Regex.Replace(safeSource, @"<(?:input|img)\b[^>]*>", m => new string(' ', m.Length), RegexOptions.IgnoreCase);
        var document = Markdown.Parse(safeSource, Pipeline); var images = new List<string>(); using var output = new StringWriter(); var renderer = new HtmlRenderer(output); Pipeline.Setup(renderer);
        var words = new WordLookup(speech.Words); renderer.ObjectRenderers.Insert(0, new LiteralRenderer(words)); renderer.ObjectRenderers.Insert(0, new LinkRenderer(images)); renderer.ObjectRenderers.Insert(0, new SourceListRenderer(source, words)); renderer.ObjectRenderers.Insert(0, new RawHtmlRenderer(safeSource, words)); renderer.ObjectRenderers.Insert(0, new MathInlineRenderer()); renderer.ObjectRenderers.Insert(0, new MathBlockRenderer());
        renderer.Render(document);
        var sanitizer = new HtmlSanitizer { KeepChildNodes = true }; sanitizer.AllowedTags.Clear(); sanitizer.AllowedAttributes.Clear(); sanitizer.AllowedCssProperties.Clear(); sanitizer.AllowedSchemes.Clear();
        foreach (var tag in "p br h1 h2 h3 h4 h5 h6 strong em del s blockquote ul ol li input table thead tbody tr th td pre code hr a span div sup sub section details summary kbd".Split(' ')) sanitizer.AllowedTags.Add(tag);
        foreach (var attribute in "class href title id start value type checked disabled align data-image-index data-tex data-reader-word-index".Split(' ')) sanitizer.AllowedAttributes.Add(attribute);
        foreach (var scheme in new[] { "https", "http", "mailto" }) sanitizer.AllowedSchemes.Add(scheme);
        return new(sanitizer.Sanitize(output.ToString()), images);
    }
    private sealed class LiteralRenderer(WordLookup words) : HtmlObjectRenderer<LiteralInline>
    {
        protected override void Write(HtmlRenderer renderer, LiteralInline obj)
        {
            var text = obj.Content.ToString(); int cursor = 0;
            foreach (Match m in SpeechText.WordPattern.Matches(text))
            {
                renderer.WriteEscape(text[cursor..m.Index]); int sourceStart = obj.Span.Start + m.Index + (obj.IsFirstCharacterEscaped ? 1 : 0);
                int index = words.Overlapping(sourceStart, m.Length);
                if (index >= 0) renderer.Write($"<span data-reader-word-index=\"{index}\">"); renderer.WriteEscape(m.Value); if (index >= 0) renderer.Write("</span>"); cursor = m.Index + m.Length;
            }
            renderer.WriteEscape(text[cursor..]);
        }
    }
    private sealed class LinkRenderer(List<string> images) : HtmlObjectRenderer<LinkInline>
    {
        protected override void Write(HtmlRenderer renderer, LinkInline obj)
        {
            var url = obj.Url ?? "";
            if (obj.IsImage)
            {
                var safe = Uri.TryCreate(url, UriKind.Absolute, out var uri) && uri.Scheme == "https" && string.IsNullOrEmpty(uri.UserInfo);
                if (safe) { var index = images.Count; images.Add(url); renderer.Write($"<span class=\"md-image\" data-image-index=\"{index}\">[Image: "); renderer.WriteChildren(obj); renderer.Write(" — load images to view]</span>"); }
                else { renderer.Write("<span class=\"md-image-blocked\">[Image: "); renderer.WriteChildren(obj); renderer.Write(" — unavailable]</span>"); }
                return;
            }
            renderer.Write("<a href=\""); renderer.WriteEscape(url); renderer.Write("\">"); renderer.WriteChildren(obj); renderer.Write("</a>");
        }
    }
    // Matched in place at the line start; copying the rest of the document per list item was quadratic.
    private static readonly Regex OrderedMarker = new(@"\G\s*(\d{1,3})[.)]\s+");
    private sealed class SourceListRenderer(string source, WordLookup words) : HtmlObjectRenderer<ListBlock>
    {
        protected override void Write(HtmlRenderer renderer, ListBlock list)
        {
            renderer.WriteLine(list.IsOrdered ? "<ol>" : "<ul>");
            foreach (var obj in list.OfType<ListItemBlock>())
            {
                renderer.Write("<li>");
                if (list.IsOrdered)
                {
                    var lineStart = obj.Span.Start <= 0 ? 0 : source.LastIndexOf('\n', Math.Min(obj.Span.Start - 1, source.Length - 1)) + 1;
                    var match = OrderedMarker.Match(source, lineStart);
                    if (match.Success) { int offset = match.Groups[1].Index; var index = words.StartingAt(offset); renderer.Write($"<span class=\"ordered-marker\" data-reader-word-index=\"{index}\">{match.Groups[1].Value}.</span> "); }
                }
                var previous = renderer.ImplicitParagraph;
                renderer.ImplicitParagraph = !list.IsLoose;
                try { renderer.WriteChildren(obj); } finally { renderer.ImplicitParagraph = previous; }
                renderer.WriteLine("</li>");
            }
            renderer.WriteLine(list.IsOrdered ? "</ol>" : "</ul>");
        }
    }
    private sealed class RawHtmlRenderer(string source, WordLookup words) : HtmlObjectRenderer<HtmlBlock>
    {
        protected override void Write(HtmlRenderer renderer, HtmlBlock obj)
        {
            var start = Math.Clamp(obj.Span.Start, 0, source.Length); var end = Math.Clamp(obj.Span.End + 1, start, source.Length); var html = source[start..end];
            foreach (Match part in Regex.Matches(html, @"<[^>]*>|&(?:#\d+|#x[0-9a-fA-F]+|\w+);|[^<&]+|[<&]"))
            {
                if (part.Value.StartsWith('<') || part.Value.StartsWith('&')) { renderer.Write(part.Value); continue; }
                int cursor = 0; foreach (Match word in SpeechText.WordPattern.Matches(part.Value))
                {
                    renderer.WriteEscape(part.Value[cursor..word.Index]); int offset = start + part.Index + word.Index; var index = words.StartingAt(offset);
                    if (index >= 0) renderer.Write($"<span data-reader-word-index=\"{index}\">"); renderer.WriteEscape(word.Value); if (index >= 0) renderer.Write("</span>"); cursor = word.Index + word.Length;
                }
                renderer.WriteEscape(part.Value[cursor..]);
            }
        }
    }
    // Indexed once per render; a linear scan per word made long documents quadratic.
    private sealed class WordLookup
    {
        private readonly IReadOnlyList<SpeechWord> words; private readonly Dictionary<int, int> starts = []; private readonly bool sorted = true;
        public WordLookup(IReadOnlyList<SpeechWord> words)
        {
            this.words = words;
            for (int i = 0; i < words.Count; i++) { starts.TryAdd(words[i].SourceStart, i); if (i > 0 && (words[i].SourceStart < words[i - 1].SourceStart || words[i].SourceEnd < words[i - 1].SourceEnd)) sorted = false; }
        }
        public int StartingAt(int offset) => starts.TryGetValue(offset, out var index) ? index : -1;
        // First word that overlaps [start, start + length), or that contains start.
        public int Overlapping(int start, int length)
        {
            bool Hit(SpeechWord w) => w.SourceStart <= start && w.SourceEnd > start || w.SourceStart >= start && w.SourceStart < start + length;
            if (!sorted) { for (int i = 0; i < words.Count; i++) if (Hit(words[i])) return i; return -1; }
            int low = 0, high = words.Count;
            while (low < high) { var middle = low + (high - low) / 2; if (words[middle].SourceEnd <= start) low = middle + 1; else high = middle; }
            return low < words.Count && Hit(words[low]) ? low : -1;
        }
    }
    private sealed class MathInlineRenderer : HtmlObjectRenderer<MathInline>
    {
        protected override void Write(HtmlRenderer renderer, MathInline obj) => renderer.Write("<span class=\"" + (obj.DelimiterCount >= 2 ? "math-block" : "math-inline") + "\" data-tex=\"" + WebUtility.HtmlEncode(obj.Content.ToString()) + "\"></span>");
    }
    private sealed class MathBlockRenderer : HtmlObjectRenderer<MathBlock>
    {
        protected override void Write(HtmlRenderer renderer, MathBlock obj) => renderer.Write("<div class=\"math-block\" data-tex=\"" + WebUtility.HtmlEncode(obj.Lines.ToString()) + "\"></div>");
    }
}
