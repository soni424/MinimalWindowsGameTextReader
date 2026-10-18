using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Text.RegularExpressions;
using GameTextReader.Core;
using Windows.Graphics.Imaging;
using Windows.Media.Ocr;
using Windows.Storage.Streams;
using WinLanguage = Windows.Globalization.Language;

namespace GameTextReader.Windows;

public sealed record OcrLineData(string Text, double X, double Y, double Width, double Height);
public sealed record RecognitionResult(string Text, string Variant, int Attempts, double ElapsedMs);
public sealed class OcrService
{
    private readonly SemaphoreSlim serial = new(1); private OcrEngine? engine;
    public async Task<RecognitionResult> RecognizeAsync(Bitmap image, string mode, CancellationToken cancellation)
    {
        await serial.WaitAsync(cancellation);
        try
        {
            engine ??= OcrEngine.TryCreateFromLanguage(new WinLanguage("en-US")) ?? OcrEngine.TryCreateFromUserProfileLanguages() ?? throw new InvalidOperationException("Install a Windows OCR language pack in Settings → Time & language.");
            var clock = Stopwatch.StartNew(); var lines = await RecognizeOne(image, cancellation); var baseline = Layout(lines); var best = baseline; var variant = "original"; var attempts = 1;
            bool retry = baseline.Length == 0 && Contrast(image) >= 8 || Evidence(baseline) < .88 || lines.Count > 0 && Median(lines.Select(l => l.Height)) < 14 || Contrast(image) < 70;
            if (mode == "enhanced" && retry)
            {
                var limit = (int)Math.Min(4096, OcrEngine.MaxImageDimension); var scale = Math.Min(2, Math.Min((double)limit / Math.Max(image.Width, image.Height), Math.Sqrt(6_000_000d / (image.Width * image.Height))));
                foreach (var gray in new[] { false, true })
                {
                    cancellation.ThrowIfCancellationRequested(); using var prepared = Prepare(image, scale, gray);
                    try
                    {
                        var candidate = Layout(await RecognizeOne(prepared, cancellation)); attempts++;
                        var alpha = candidate.Count(char.IsLetter); var evidence = Evidence(candidate);
                        var emptyGood = Regex.Matches(candidate, @"\p{L}{3,}").Count >= 2 && alpha >= 8 && evidence >= .88;
                        if (candidate.Length > 0 && !candidate.Contains('�') && alpha >= baseline.Count(char.IsLetter) * .8 && evidence >= .65 && (baseline.Length == 0 ? emptyGood : evidence > Evidence(best) + .12)) { best = candidate; variant = gray ? "contrast_gray" : "enlarged_color"; }
                    }
                    catch (Exception e) when (e is not OperationCanceledException) { /* Optional passes cannot erase a successful baseline. */ }
                }
            }
            return new(best, variant, attempts, clock.Elapsed.TotalMilliseconds);
        }
        finally { serial.Release(); }
    }
    private async Task<IReadOnlyList<OcrLineData>> RecognizeOne(Bitmap image, CancellationToken cancellation)
    {
        using var memory = new MemoryStream(); image.Save(memory, ImageFormat.Png); var bytes = memory.ToArray();
        using var stream = new InMemoryRandomAccessStream(); using (var writer = new DataWriter(stream)) { writer.WriteBytes(bytes); await writer.StoreAsync(); writer.DetachStream(); }
        stream.Seek(0); var decoder = await BitmapDecoder.CreateAsync(stream); using var bitmap = await decoder.GetSoftwareBitmapAsync(BitmapPixelFormat.Bgra8, BitmapAlphaMode.Premultiplied);
        cancellation.ThrowIfCancellationRequested(); var result = await engine!.RecognizeAsync(bitmap); cancellation.ThrowIfCancellationRequested();
        return result.Lines.Where(l => !string.IsNullOrWhiteSpace(l.Text)).Select(l => { var rects = l.Words.Select(w => w.BoundingRect).ToArray(); return new OcrLineData(l.Text, rects.Min(r => r.X), rects.Min(r => r.Y), rects.Max(r => r.Right) - rects.Min(r => r.X), rects.Max(r => r.Bottom) - rects.Min(r => r.Y)); }).ToArray();
    }
    public static string Layout(IReadOnlyList<OcrLineData> lines)
    {
        if (lines.Count == 0) return ""; var height = Median(lines.Select(l => l.Height)); var gaps = lines.Zip(lines.Skip(1), (a, b) => b.Y - (a.Y + a.Height)).ToArray(); var nonnegative = gaps.Where(g => g >= 0).ToArray();
        var threshold = Math.Max(12, nonnegative.Length <= 2 ? height * 1.75 : Math.Max(height * 1.5, Median(nonnegative) + height * .5));
        var result = new System.Text.StringBuilder(); for (int i = 0; i < lines.Count; i++) { result.Append(Regex.Replace(lines[i].Text.Trim(), @"[ \t]+", " ")); if (i < gaps.Length) result.Append(gaps[i] >= threshold ? "\n\n" : "\n"); }
        return result.ToString();
    }
    private static double Median(IEnumerable<double> values) { var a = values.Order().ToArray(); return a.Length == 0 ? 0 : a.Length % 2 != 0 ? a[a.Length / 2] : (a[a.Length / 2 - 1] + a[a.Length / 2]) / 2; }
    public static double Evidence(string text)
    {
        var words = Regex.Matches(text, @"\p{L}+"); if (words.Count == 0) return 0;
        var fragments = words.Count(w => w.Length == 1 && !new[] { "a", "i" }.Contains(w.Value.ToLowerInvariant())); var broken = Regex.Matches(text, @"[A-Za-z][0-9|~{}][A-Za-z]|[0-9|~{}][A-Za-z]{2}").Count;
        return Math.Max(0, 1 - .6 * fragments / words.Count - .35 * broken / words.Count - .2 * text.Count(c => "|~{}�".Contains(c)) / words.Count);
    }
    public static byte[] Fingerprint(Bitmap bitmap)
    {
        using var tiny = new Bitmap(bitmap, 64, 32); var data = new byte[2048]; for (int y = 0; y < 32; y++) for (int x = 0; x < 64; x++) { var c = tiny.GetPixel(x, y); data[y * 64 + x] = (byte)((c.R * 299 + c.G * 587 + c.B * 114) / 1000); }
        return data;
    }
    public static double Difference(byte[] a, byte[] b) => a.Zip(b, (x, y) => Math.Abs(x - y)).Average();
    private static int Contrast(Bitmap bitmap) { var values = Fingerprint(bitmap).Order().ToArray(); return values[(int)(values.Length * .98)] - values[(int)(values.Length * .02)]; }
    private static Bitmap Prepare(Bitmap bitmap, double scale, bool gray)
    {
        var target = new Bitmap(Math.Max(1, (int)(bitmap.Width * scale)), Math.Max(1, (int)(bitmap.Height * scale)), PixelFormat.Format32bppArgb);
        using (var g = Graphics.FromImage(target)) { g.InterpolationMode = InterpolationMode.HighQualityBicubic; g.DrawImage(bitmap, 0, 0, target.Width, target.Height); }
        var data = target.LockBits(new Rectangle(0, 0, target.Width, target.Height), ImageLockMode.ReadWrite, PixelFormat.Format32bppArgb);
        try
        {
            var bytes = new byte[data.Stride * target.Height]; Marshal.Copy(data.Scan0, bytes, 0, bytes.Length);
            if (gray)
            {
                var histogram = new int[256]; for (int i = 0; i < bytes.Length; i += 4) { int value = (bytes[i + 2] * 299 + bytes[i + 1] * 587 + bytes[i] * 114) / 1000; histogram[value]++; bytes[i] = bytes[i + 1] = bytes[i + 2] = (byte)value; }
                int total = target.Width * target.Height, low = 0, high = 255, count = 0;
                for (int i = 0; i < 256; i++) { count += histogram[i]; if (count >= total * .01) { low = i; break; } }
                count = 0; for (int i = 255; i >= 0; i--) { count += histogram[i]; if (count >= total * .01) { high = i; break; } }
                for (int i = 0; i < bytes.Length; i += 4) bytes[i] = bytes[i + 1] = bytes[i + 2] = (byte)Math.Clamp((bytes[i] - low) * 255 / Math.Max(1, high - low), 0, 255);
            }
            var original = (byte[])bytes.Clone();
            for (int y = 1; y < target.Height - 1; y++) for (int x = 1; x < target.Width - 1; x++) for (int c = 0; c < 3; c++) { int i = y * data.Stride + x * 4 + c; int delta = 4 * original[i] - original[i - 4] - original[i + 4] - original[i - data.Stride] - original[i + data.Stride]; if (Math.Abs(delta) > 2) bytes[i] = (byte)Math.Clamp(original[i] + delta / 4, 0, 255); }
            Marshal.Copy(bytes, 0, data.Scan0, bytes.Length);
        }
        finally { target.UnlockBits(data); }
        return target;
    }
}
