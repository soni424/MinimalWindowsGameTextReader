namespace GameTextReader.Core;

public sealed record MonitorInfo(string Device, int Left, int Top, int Right, int Bottom, int DpiX = 96, int DpiY = 96, bool Primary = false)
{
    public int Width => Right - Left; public int Height => Bottom - Top;
    public bool Contains(int x, int y) => x >= Left && x < Right && y >= Top && y < Bottom;
    public int[] Bounds => [Left, Top, Right, Bottom];
}
public static class CaptureGeometry
{
    public static bool Valid(int[]? b) => b is { Length: 4 } && b[2] > b[0] && b[3] > b[1];
    public static long Intersection(int[] box, MonitorInfo m) => (long)Math.Max(0, Math.Min(box[2], m.Right) - Math.Max(box[0], m.Left)) * Math.Max(0, Math.Min(box[3], m.Bottom) - Math.Max(box[1], m.Top));
    public static CaptureArea Build(int[] box, IReadOnlyList<MonitorInfo> monitors)
    {
        if (!Valid(box)) throw new ArgumentException("Capture area must have positive width and height.");
        var m = monitors.OrderByDescending(m => Intersection(box, m)).FirstOrDefault();
        if (m == null || Intersection(box, m) == 0) return new() { Box = box.ToArray() };
        return new() { Box = box.ToArray(), MonitorDevice = m.Device, MonitorBounds = m.Bounds, MonitorDpi = [m.DpiX, m.DpiY], RelativeBox = [Math.Round((double)(box[0] - m.Left) / m.Width, 10), Math.Round((double)(box[1] - m.Top) / m.Height, 10), Math.Round((double)(box[2] - m.Left) / m.Width, 10), Math.Round((double)(box[3] - m.Top) / m.Height, 10)] };
    }
    public static int[] Resolve(CaptureArea? area, IReadOnlyList<MonitorInfo> monitors)
    {
        if (!Valid(area?.Box)) throw new InvalidOperationException("No capture area is saved for this profile.");
        if (area!.RelativeBox is not { Length: 4 } relative || string.IsNullOrEmpty(area.MonitorDevice))
        {
            var b = area.Box; if (new[] { (b[0], b[1]), (b[2] - 1, b[1]), (b[0], b[3] - 1), (b[2] - 1, b[3] - 1) }.All(p => monitors.Any(m => m.Contains(p.Item1, p.Item2)))) return b.ToArray();
            throw new InvalidOperationException("The saved capture area is outside the connected displays. Edit the area to restore it.");
        }
        var monitor = monitors.FirstOrDefault(m => m.Device.Equals(area.MonitorDevice, StringComparison.OrdinalIgnoreCase)) ?? throw new InvalidOperationException($"The saved monitor {area.MonitorDevice} is not connected. Edit the capture area.");
        int[] resolved = [monitor.Left + (int)Math.Round(relative[0] * monitor.Width), monitor.Top + (int)Math.Round(relative[1] * monitor.Height), monitor.Left + (int)Math.Round(relative[2] * monitor.Width), monitor.Top + (int)Math.Round(relative[3] * monitor.Height)];
        if (!Valid(resolved) || Intersection(resolved, monitor) == 0) throw new InvalidOperationException("The saved capture area no longer overlaps its display.");
        return resolved;
    }
}
