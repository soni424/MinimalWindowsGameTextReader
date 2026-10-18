using System.Windows;
using System.Windows.Media;
using ICSharpCode.AvalonEdit.Document;
using ICSharpCode.AvalonEdit.Rendering;

namespace GameTextReader.App;

public sealed class ReadingMarker : DocumentColorizingTransformer, IBackgroundRenderer
{
    private static readonly Brush DarkRow = Frozen(Color.FromArgb(90, 51, 119, 188));
    private static readonly Brush LightRow = Frozen(Color.FromArgb(95, 135, 195, 250));
    private static readonly Brush Word = Frozen(Color.FromRgb(249, 207, 65));
    private static readonly Brush WordText = Frozen(Color.FromRgb(22, 29, 38));
    private static Brush Frozen(Color color) { var brush = new SolidColorBrush(color); brush.Freeze(); return brush; }
    public int Start { get; set; } = -1; public int End { get; set; }
    public bool Dark { get; set; } = true;
    public KnownLayer Layer => KnownLayer.Selection;
    public void Draw(TextView view, DrawingContext context)
    {
        if (Start < 0 || End <= Start || view.Document == null || End > view.Document.TextLength) return;
        view.EnsureVisualLines(); var rectangles = BackgroundGeometryBuilder.GetRectsForSegment(view, new TextSegment { StartOffset = Start, Length = End - Start }).ToArray();
        if (rectangles.Length == 0) return;
        var first = rectangles[0]; var row = new Rect(0, first.Top, view.ActualWidth, first.Height);
        context.DrawRectangle(Dark ? DarkRow : LightRow, null, row);
        foreach (var rect in rectangles) context.DrawRoundedRectangle(Word, null, rect, 2, 2);
    }
    protected override void ColorizeLine(DocumentLine line)
    {
        if (Start < 0 || End <= Start || Start >= line.EndOffset || End <= line.Offset) return;
        ChangeLinePart(Math.Max(Start, line.Offset), Math.Min(End, line.EndOffset), element => element.TextRunProperties.SetForegroundBrush(WordText));
    }
}
