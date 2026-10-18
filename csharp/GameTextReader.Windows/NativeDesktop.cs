using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using GameTextReader.Core;
using Microsoft.Win32;

namespace GameTextReader.Windows;

public static class NativeDesktop
{
    [StructLayout(LayoutKind.Sequential)] private struct KeyboardInput { public ushort Key, Scan; public uint Flags, Time; public nuint Extra; }
    [StructLayout(LayoutKind.Explicit, Size = 32)] private struct InputUnion { [FieldOffset(0)] public KeyboardInput Keyboard; }
    [StructLayout(LayoutKind.Sequential)] private struct Input { public uint Type; public InputUnion Data; }
    [DllImport("user32.dll", SetLastError = true)] private static extern uint SendInput(uint count, Input[] input, int size);
    public static void SendChord(ushort[] modifiers, ushort key)
    {
        var events = new List<Input>();
        void Key(ushort value, bool up) => events.Add(new() { Type = 1, Data = new() { Keyboard = new() { Key = value, Flags = up ? 2u : 0 } } });
        foreach (var modifier in modifiers) Key(modifier, false); Key(key, false); Key(key, true); foreach (var modifier in modifiers.Reverse()) Key(modifier, true);
        if (SendInput((uint)events.Count, events.ToArray(), Marshal.SizeOf<Input>()) != events.Count) throw new InvalidOperationException("Windows rejected shortcut input (error " + Marshal.GetLastWin32Error() + ").");
    }
    [StructLayout(LayoutKind.Sequential)] public struct Rect { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] private struct MonitorData { public int Size; public Rect Monitor, Work; public uint Flags; [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string Device; }
    private delegate bool MonitorCallback(nint monitor, nint dc, nint rect, nint data);
    [DllImport("user32.dll")] private static extern bool EnumDisplayMonitors(nint dc, nint clip, MonitorCallback callback, nint data);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern bool GetMonitorInfo(nint monitor, ref MonitorData data);
    [DllImport("shcore.dll")] private static extern int GetDpiForMonitor(nint monitor, int type, out uint x, out uint y);
    [DllImport("user32.dll")] public static extern nint GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool IsWindow(nint hwnd);
    [DllImport("user32.dll")] public static extern bool IsIconic(nint hwnd);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(nint hwnd);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(nint hwnd, out Rect rect);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(nint hwnd, out uint pid);
    [DllImport("user32.dll")] public static extern bool GetCursorPos(out Point point);
    [DllImport("user32.dll", SetLastError = true)] public static extern bool SetWindowPos(nint hwnd, nint after, int x, int y, int width, int height, uint flags);
    [DllImport("user32.dll")] private static extern nint GetDC(nint window);
    [DllImport("user32.dll")] private static extern int ReleaseDC(nint window, nint dc);
    [DllImport("gdi32.dll", SetLastError = true)] private static extern bool BitBlt(nint target, int x, int y, int width, int height, nint source, int sourceX, int sourceY, uint operation);
    [DllImport("dwmapi.dll")] private static extern int DwmFlush();
    // Screen captures read the last composed frame. Wait for the next one so a
    // window that was just closed or minimized is no longer in the screenshot.
    public static void FlushCompositor() { try { DwmFlush(); } catch (DllNotFoundException) { } }
    public static IReadOnlyList<MonitorInfo> Monitors()
    {
        var result = new List<MonitorInfo>();
        EnumDisplayMonitors(0, 0, (handle, _, _, _) => { var data = new MonitorData { Size = Marshal.SizeOf<MonitorData>(), Device = "" }; if (GetMonitorInfo(handle, ref data)) { uint x = 96, y = 96; try { GetDpiForMonitor(handle, 0, out x, out y); } catch (DllNotFoundException) { } var r = data.Monitor; result.Add(new(data.Device, r.Left, r.Top, r.Right, r.Bottom, (int)x, (int)y, (data.Flags & 1) != 0)); } return true; }, 0);
        return result;
    }
    public static Bitmap Capture(int[] box)
    {
        if (!CaptureGeometry.Valid(box)) throw new ArgumentException("Invalid capture area.");
        var bitmap = new Bitmap(box[2] - box[0], box[3] - box[1], PixelFormat.Format32bppRgb);
        try
        {
            using var graphics = Graphics.FromImage(bitmap); var source = GetDC(0); nint target = 0;
            if (source == 0) throw new System.ComponentModel.Win32Exception("Windows could not access the screen device context.");
            try
            {
                target = graphics.GetHdc();
                // Graphics.CopyFromScreen rejects this combined raster operation
                // even though BitBlt supports SRCCOPY | CAPTUREBLT (as used by mss).
                if (!BitBlt(target, 0, 0, bitmap.Width, bitmap.Height, source, box[0], box[1], 0x40CC0020)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "Screen capture failed.");
            }
            finally { if (target != 0) graphics.ReleaseHdc(target); ReleaseDC(0, source); }
            return bitmap;
        }
        catch { bitmap.Dispose(); throw; }
    }
    public static (nint handle, uint pid)? Foreground(int[] box)
    {
        var h = GetForegroundWindow(); if (h == 0 || !IsWindowVisible(h) || IsIconic(h) || !GetWindowRect(h, out var r)) return null;
        GetWindowThreadProcessId(h, out var pid); if (pid == Environment.ProcessId || r.Left > box[0] || r.Top > box[1] || r.Right < box[2] || r.Bottom < box[3]) return null;
        return (h, pid);
    }
    public static bool DarkSystemTheme() => Registry.GetValue(@"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "AppsUseLightTheme", 1) is int value && value == 0;
}

public interface IStartupStore
{
    string? Read();
    void Write(string? command);
}
public sealed class StartupRegistration(IStartupStore? store = null)
{
    private const string Key = @"Software\Microsoft\Windows\CurrentVersion\Run";
    public const string Name = "GameTextReader";
    public static string Command(string executable) => "\"" + Path.GetFullPath(executable).Replace("\"", "") + "\" --startup";
    public bool IsEnabled() { if (store != null) return store.Read() != null; using var key = Registry.CurrentUser.OpenSubKey(Key); return key?.GetValue(Name) is string; }
    public void SetEnabled(bool enabled, string executable)
    {
        if (store != null) { store.Write(enabled ? Command(executable) : null); return; }
        using var key = Registry.CurrentUser.CreateSubKey(Key, true) ?? throw new IOException("Windows denied access to the current-user startup key.");
        if (enabled) key.SetValue(Name, Command(executable)); else key.DeleteValue(Name, false);
    }
}

public sealed record ParsedHotkey(string Display, uint Modifiers, uint Key);
public static class HotkeyParser
{
    public static ParsedHotkey? Parse(string text)
    {
        if (text == "") return null; var parts = text.Split('+', StringSplitOptions.TrimEntries); uint modifiers = 0; uint key = 0; string name = "";
        var labels = new List<(int order, string text)>();
        foreach (var part in parts)
        {
            var value = part.ToLowerInvariant(); var modifier = value switch { "ctrl" or "control" => (2u, 0, "Ctrl"), "alt" => (1u, 1, "Alt"), "shift" => (4u, 2, "Shift"), "win" or "windows" => (8u, 3, "Win"), _ => (0u, 0, "") };
            if (modifier.Item1 != 0) { if ((modifiers & modifier.Item1) != 0) throw new ArgumentException("A shortcut key can appear only once."); modifiers |= modifier.Item1; labels.Add((modifier.Item2, modifier.Item3)); continue; }
            if (key != 0) throw new ArgumentException("Use exactly one trigger key.");
            if (value.StartsWith('f') && int.TryParse(value[1..], out var f) && f is >= 1 and <= 24) { if (f == 12) throw new ArgumentException("F12 is reserved by Windows."); key = (uint)(0x70 + f - 1); name = "F" + f; }
            else if (part.Length == 1 && char.IsAsciiLetterOrDigit(part[0])) { key = char.ToUpperInvariant(part[0]); name = ((char)key).ToString(); }
            else { var special = value switch { "space" => (0x20u, "Space"), "enter" => (0x0Du, "Enter"), "tab" => (9u, "Tab"), "esc" or "escape" => (0x1Bu, "Esc"), "insert" => (0x2Du, "Insert"), "delete" => (0x2Eu, "Delete"), "home" => (0x24u, "Home"), "end" => (0x23u, "End"), "pageup" => (0x21u, "PageUp"), "pagedown" => (0x22u, "PageDown"), "left" => (0x25u, "Left"), "up" => (0x26u, "Up"), "right" => (0x27u, "Right"), "down" => (0x28u, "Down"), ";" => (0xBAu, ";"), "=" => (0xBBu, "="), "," => (0xBCu, ","), "-" => (0xBDu, "-"), "." => (0xBEu, "."), "/" => (0xBFu, "/"), "`" => (0xC0u, "`"), "[" => (0xDBu, "["), "\\" => (0xDCu, "\\"), "]" => (0xDDu, "]"), "'" => (0xDEu, "'"), _ => throw new ArgumentException("Unsupported shortcut key: " + part) }; key = special.Item1; name = special.Item2; }
        }
        if (key == 0 || modifiers == 0 && !name.StartsWith('F')) throw new ArgumentException("Add a modifier, or use a function key.");
        return new(string.Join('+', labels.OrderBy(l => l.order).Select(l => l.text).Append(name)), modifiers, key);
    }
}

public sealed class HotkeyRegistration : IDisposable
{
    // Own the native message queue rather than depending on WPF/WebView keyboard
    // preprocessing. Registration, replacement, and cleanup all run on this thread.
    [StructLayout(LayoutKind.Sequential)]
    private struct Message
    {
        public nint Window; public uint Id; public nuint WParam; public nint LParam;
        public uint Time; public int X, Y; public uint Private;
    }
    [DllImport("user32.dll", SetLastError = true)] private static extern bool RegisterHotKey(nint hwnd, int id, uint modifiers, uint key);
    [DllImport("user32.dll")] private static extern bool UnregisterHotKey(nint hwnd, int id);
    [DllImport("kernel32.dll")] private static extern uint GetCurrentThreadId();
    [DllImport("user32.dll", EntryPoint = "GetMessageW", SetLastError = true)] private static extern int GetMessage(out Message message, nint window, uint minimum, uint maximum);
    [DllImport("user32.dll", EntryPoint = "PeekMessageW")] private static extern bool PeekMessage(out Message message, nint window, uint minimum, uint maximum, uint remove);
    [DllImport("user32.dll", EntryPoint = "PostThreadMessageW", SetLastError = true)] private static extern bool PostThreadMessage(uint thread, uint message, nuint wp, nint lp);
    private readonly System.Collections.Concurrent.ConcurrentQueue<Action> commands = new();
    private readonly Thread thread; private readonly TaskCompletionSource<uint> ready = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private List<ParsedHotkey?> active = []; private int disposed;
    public event Action<string>? Log;
    public event Action<int>? Fired;
    public HotkeyRegistration()
    {
        thread = new Thread(Run) { IsBackground = true, Name = "Game Text Reader shortcuts" }; thread.Start();
        ready.Task.WaitAsync(TimeSpan.FromSeconds(5)).GetAwaiter().GetResult();
    }
    private void Run()
    {
        PeekMessage(out _, 0, 0, 0, 0); ready.TrySetResult(GetCurrentThreadId());
        try
        {
            while (true)
            {
                int result = GetMessage(out var message, 0, 0, 0);
                if (result <= 0) break;
                if (message.Id == 0x8001) { while (commands.TryDequeue(out var command)) command(); }
                else if (message.Id == 0x0312 && Volatile.Read(ref disposed) == 0)
                {
                    var index = (int)message.WParam - 0x4200;
                    if (index >= 0 && index < active.Count && active[index] != null)
                        try { Log?.Invoke("Dispatched " + active[index]!.Display); Fired?.Invoke(index); }
                        catch (Exception error) { Log?.Invoke("Shortcut callback failed: " + error.Message); }
                }
            }
        }
        finally { Unregister(); }
    }
    public void Apply(IEnumerable<string> keys)
    {
        var parsed = keys.Select(HotkeyParser.Parse).ToList(); var assigned = parsed.Where(p => p != null).ToArray();
        if (assigned.Select(p => p!.Display).Distinct().Count() != assigned.Length) throw new ArgumentException("Two actions cannot use the same shortcut.");
        ObjectDisposedException.ThrowIf(Volatile.Read(ref disposed) != 0, this);
        var completed = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        commands.Enqueue(() => { try { ApplyOnWorker(parsed); completed.TrySetResult(); } catch (Exception error) { completed.TrySetException(error); } });
        if (!PostThreadMessage(ready.Task.Result, 0x8001, 0, 0)) throw new InvalidOperationException("The shortcut message queue is unavailable.");
        completed.Task.WaitAsync(TimeSpan.FromSeconds(5)).GetAwaiter().GetResult();
    }
    private void ApplyOnWorker(List<ParsedHotkey?> parsed)
    {
        var previous = active; Unregister();
        try { Register(parsed); active = parsed; }
        catch (Exception error) { Unregister(parsed); try { Register(previous); active = previous; } catch { active = []; throw new InvalidOperationException(error.Message + " Previous shortcuts could not be restored."); } throw; }
    }
    private void Register(List<ParsedHotkey?> values)
    {
        for (int i = 0; i < values.Count; i++) if (values[i] is { } key) { if (!RegisterHotKey(0, 0x4200 + i, key.Modifiers | 0x4000, key.Key)) throw new InvalidOperationException($"Windows could not register {key.Display} (error {Marshal.GetLastWin32Error()}). It may be in use by another app."); Log?.Invoke("Registered " + key.Display); }
    }
    private void Unregister(List<ParsedHotkey?>? values = null) { var v = values ?? active; for (int i = 0; i < v.Count; i++) if (v[i] != null) UnregisterHotKey(0, 0x4200 + i); }
    public void Dispose() { if (Interlocked.Exchange(ref disposed, 1) != 0) return; PostThreadMessage(ready.Task.Result, 0x0012, 0, 0); if (Thread.CurrentThread != thread) thread.Join(5000); }
}
