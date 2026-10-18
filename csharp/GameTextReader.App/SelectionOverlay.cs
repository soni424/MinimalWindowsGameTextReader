using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Shapes;
using GameTextReader.Core;
using GameTextReader.Windows;

namespace GameTextReader.App;

public sealed class SelectionOverlay
{
    private sealed class Pane(MonitorInfo monitor, Window window, Canvas canvas) { public MonitorInfo Monitor = monitor; public Window Window = window; public Canvas Canvas = canvas; public Border? Panel; public TextBlock? Dimensions; }
    private readonly List<Pane> panes = []; private readonly TaskCompletionSource<int[]?> completion = new(TaskCreationOptions.RunContinuationsAsynchronously); private readonly bool editor;
    private int[]? box; private MonitorInfo active; private System.Windows.Point origin; private int[]? original; private string drag = ""; private bool finished;
    public static bool IsOpen { get; private set; }
    public static Task<int[]?> SelectAsync(int[]? initial, bool editor)
    {
        if (IsOpen) return Task.FromResult<int[]?>(null); IsOpen = true; var overlay = new SelectionOverlay(initial, editor); overlay.Open(); return overlay.completion.Task;
    }
    private SelectionOverlay(int[]? initial, bool edit)
    {
        editor = edit; box = initial?.ToArray(); var monitors = NativeDesktop.Monitors(); NativeDesktop.GetCursorPos(out var cursor);
        active = box == null ? monitors.FirstOrDefault(m => m.Contains(cursor.X, cursor.Y)) ?? monitors[0] : monitors.OrderByDescending(m => CaptureGeometry.Intersection(box, m)).First();
        foreach (var monitor in monitors)
        {
            var canvas = new Canvas { Background = new SolidColorBrush(Color.FromArgb(65, 0, 0, 0)), ClipToBounds = true };
            var window = new Window { WindowStyle = WindowStyle.None, ResizeMode = ResizeMode.NoResize, AllowsTransparency = true, Background = Brushes.Transparent, Topmost = true, ShowInTaskbar = false, Content = canvas, Cursor = Cursors.Cross, Width = monitor.Width * 96d / monitor.DpiX, Height = monitor.Height * 96d / monitor.DpiY };
            var pane = new Pane(monitor, window, canvas); panes.Add(pane);
            window.SourceInitialized += (_, _) => { var hwnd = new WindowInteropHelper(window).Handle; NativeDesktop.SetWindowPos(hwnd, -1, monitor.Left, monitor.Top, monitor.Width, monitor.Height, 0x0040); };
            window.SizeChanged += (_, _) => Draw(); window.PreviewKeyDown += Key; window.Closing += (_, e) => { if (!finished) { e.Cancel = true; window.Dispatcher.BeginInvoke(() => Complete(null)); } }; // WPF cannot close a window from its own Closing event.
            canvas.MouseLeftButtonDown += (_, e) => Down(pane, e); canvas.MouseMove += (_, e) => Move(pane, e); canvas.MouseLeftButtonUp += (_, e) => Up(pane, e);
        }
    }
    private void Open() { try { foreach (var pane in panes) pane.Window.Show(); Draw(); panes.First(p => p.Monitor == active).Window.Activate(); } catch { Complete(null); throw; } }
    private System.Windows.Point ScreenPoint(MouseEventArgs e, Pane pane) => pane.Canvas.PointToScreen(e.GetPosition(pane.Canvas));
    private void Down(Pane pane, MouseButtonEventArgs e)
    {
        if (e.OriginalSource is Button || FindButton(e.OriginalSource as DependencyObject)) return;
        origin = ScreenPoint(e, pane); original = box?.ToArray(); drag = Hit(origin, pane); active = pane.Monitor;
        if (drag == "new") { box = [(int)origin.X, (int)origin.Y, (int)origin.X + 1, (int)origin.Y + 1]; original = box.ToArray(); }
        pane.Canvas.CaptureMouse(); Draw(); e.Handled = true;
    }
    private static bool FindButton(DependencyObject? item) { while (item != null) { if (item is Button) return true; item = VisualTreeHelper.GetParent(item); } return false; }
    private string Hit(System.Windows.Point point, Pane pane)
    {
        if (!editor || box == null) return "new"; var radius = 12 * pane.Monitor.DpiX / 96d; var x = point.X; var y = point.Y;
        var left = Math.Abs(x - box[0]) <= radius; var right = Math.Abs(x - box[2]) <= radius; var top = Math.Abs(y - box[1]) <= radius; var bottom = Math.Abs(y - box[3]) <= radius; var insideX = x >= box[0] - radius && x <= box[2] + radius; var insideY = y >= box[1] - radius && y <= box[3] + radius;
        if ((left || right) && insideY && (top || bottom)) return (left ? "l" : "r") + (top ? "t" : "b"); if ((left || right) && insideY) return left ? "l" : "r"; if ((top || bottom) && insideX) return top ? "t" : "b";
        return x > box[0] && x < box[2] && y > box[1] && y < box[3] ? "move" : "new";
    }
    private void Move(Pane pane, MouseEventArgs e)
    {
        var point = ScreenPoint(e, pane);
        if (!pane.Canvas.IsMouseCaptured || original == null) { pane.Window.Cursor = Hit(point, pane) switch { "move" => Cursors.SizeAll, "l" or "r" => Cursors.SizeWE, "t" or "b" => Cursors.SizeNS, "lt" or "rb" => Cursors.SizeNWSE, "rt" or "lb" => Cursors.SizeNESW, _ => Cursors.Cross }; return; }
        int x = Math.Clamp((int)point.X, active.Left, active.Right), y = Math.Clamp((int)point.Y, active.Top, active.Bottom), dx = x - (int)origin.X, dy = y - (int)origin.Y;
        if (drag == "new") box = [Math.Min((int)origin.X, x), Math.Min((int)origin.Y, y), Math.Max((int)origin.X, x), Math.Max((int)origin.Y, y)];
        else if (drag == "move") { dx = Math.Clamp(dx, active.Left - original[0], active.Right - original[2]); dy = Math.Clamp(dy, active.Top - original[1], active.Bottom - original[3]); box = [original[0] + dx, original[1] + dy, original[2] + dx, original[3] + dy]; }
        else { box = original.ToArray(); if (drag.Contains('l')) box[0] = Math.Min(x, box[2] - 2); if (drag.Contains('r')) box[2] = Math.Max(x, box[0] + 2); if (drag.Contains('t')) box[1] = Math.Min(y, box[3] - 2); if (drag.Contains('b')) box[3] = Math.Max(y, box[1] + 2); }
        Draw(); e.Handled = true;
    }
    private void Up(Pane pane, MouseButtonEventArgs e) { if (!pane.Canvas.IsMouseCaptured) return; pane.Canvas.ReleaseMouseCapture(); if (!editor && box is { } b && b[2] - b[0] >= 3 && b[3] - b[1] >= 3) Complete(box); e.Handled = true; }
    private void Key(object sender, KeyEventArgs e)
    {
        if (e.Key == System.Windows.Input.Key.Escape) { Complete(null); e.Handled = true; return; }
        if (e.Key == System.Windows.Input.Key.Enter) { if (CaptureGeometry.Valid(box)) Complete(box); e.Handled = true; return; }
        if (!editor || box == null) return; int step = Keyboard.Modifiers.HasFlag(ModifierKeys.Shift) ? 10 : 1; int dx = e.Key == System.Windows.Input.Key.Left ? -step : e.Key == System.Windows.Input.Key.Right ? step : 0, dy = e.Key == System.Windows.Input.Key.Up ? -step : e.Key == System.Windows.Input.Key.Down ? step : 0;
        if (dx == 0 && dy == 0) return; dx = Math.Clamp(dx, active.Left - box[0], active.Right - box[2]); dy = Math.Clamp(dy, active.Top - box[1], active.Bottom - box[3]); box = [box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy]; Draw(); e.Handled = true;
    }
    private void Draw()
    {
        if (finished) return;
        foreach (var pane in panes)
        {
            if (PresentationSource.FromVisual(pane.Canvas) == null) continue;
            pane.Canvas.Children.Clear(); var dpi = VisualTreeHelper.GetDpi(pane.Window); double sx = dpi.DpiScaleX, sy = dpi.DpiScaleY;
            if (box != null)
            {
                var a = pane.Canvas.PointFromScreen(new(box[0], box[1])); var b = pane.Canvas.PointFromScreen(new(box[2], box[3])); var selection = new Rectangle { Width = Math.Max(1, b.X - a.X), Height = Math.Max(1, b.Y - a.Y), Stroke = new SolidColorBrush(Color.FromRgb(76, 177, 255)), StrokeThickness = 3, Fill = new SolidColorBrush(Color.FromArgb(15, 0, 130, 255)), IsHitTestVisible = false }; Canvas.SetLeft(selection, a.X); Canvas.SetTop(selection, a.Y); pane.Canvas.Children.Add(selection);
                if (editor) foreach (var (x, y) in new[] { (a.X, a.Y), (b.X, a.Y), (a.X, b.Y), (b.X, b.Y), ((a.X + b.X) / 2, a.Y), ((a.X + b.X) / 2, b.Y), (a.X, (a.Y + b.Y) / 2), (b.X, (a.Y + b.Y) / 2) }) { var handle = new Rectangle { Width = 10, Height = 10, Fill = Brushes.White, Stroke = Brushes.DodgerBlue, StrokeThickness = 2, IsHitTestVisible = false }; Canvas.SetLeft(handle, x - 5); Canvas.SetTop(handle, y - 5); pane.Canvas.Children.Add(handle); }
            }
            if (pane.Monitor != active) continue;
            var panel = new Border { Background = new SolidColorBrush(Color.FromRgb(19, 31, 48)), BorderBrush = Brushes.SteelBlue, BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(8), Padding = new Thickness(16), Width = Math.Min(510, pane.Monitor.Width / sx - 32) };
            var stack = new StackPanel(); stack.Children.Add(new TextBlock { Text = editor ? "Set capture area" : "Select a snippet", FontSize = 18, FontWeight = FontWeights.SemiBold, Foreground = Brushes.White }); stack.Children.Add(new TextBlock { Text = editor ? "Drag outside to draw · inside to move · handles to resize\nArrow keys move 1 px · Shift moves 10 px · Enter saves · Esc cancels" : "Drag around text to read · Esc cancels", Foreground = Brushes.LightSteelBlue, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 8, 0, 6) });
            var dimensions = new TextBlock { Foreground = Brushes.White, Text = box == null ? "Draw a capture box" : $"{box[2] - box[0]} × {box[3] - box[1]} px · position {box[0]}, {box[1]}" }; stack.Children.Add(dimensions); var actions = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
            if (editor) { var save = new Button { Content = "Save area", IsEnabled = CaptureGeometry.Valid(box), Background = Brushes.DodgerBlue }; save.Click += (_, _) => Complete(box); actions.Children.Add(save); }
            var cancel = new Button { Content = "Cancel" }; cancel.Click += (_, _) => Complete(null); actions.Children.Add(cancel); stack.Children.Add(actions); panel.Child = stack; pane.Canvas.Children.Add(panel); Canvas.SetLeft(panel, Math.Max(16, (pane.Monitor.Width / sx - panel.Width) / 2)); Canvas.SetTop(panel, 20); pane.Panel = panel; pane.Dimensions = dimensions;
        }
    }
    private void Complete(int[]? result) { if (finished) return; finished = true; foreach (var pane in panes) { pane.Canvas.ReleaseMouseCapture(); pane.Window.Close(); } IsOpen = false; completion.TrySetResult(result?.ToArray()); }
}
