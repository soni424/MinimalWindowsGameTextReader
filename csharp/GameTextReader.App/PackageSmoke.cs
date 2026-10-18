using System.IO;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using GameTextReader.Windows;

namespace GameTextReader.App;

public partial class MainWindow
{
    internal async void RunPackageSmoke(string reportPath)
    {
        var checks = new Dictionary<string, object?> { ["version"] = BuildInformation(), ["runtimeDirectory"] = RuntimeEnvironment.GetRuntimeDirectory(), ["applicationDirectory"] = AppContext.BaseDirectory };
        async Task Wait(Func<bool> predicate, string operation, int timeout = 30000) { var start = Environment.TickCount64; while (!predicate() && Environment.TickCount64 - start < timeout) await Task.Delay(25); if (!predicate()) throw new InvalidOperationException(operation + " timed out."); }
        try
        {
            checks["selfContainedRuntime"] = RuntimeEnvironment.GetRuntimeDirectory().StartsWith(AppContext.BaseDirectory, StringComparison.OrdinalIgnoreCase);
            if (!(bool)checks["selfContainedRuntime"]!) throw new InvalidOperationException("The package did not load its own .NET runtime.");
            await Wait(() => Voices.Items.Count > 0, "Voice discovery"); checks["voices"] = Voices.Items.Count; checks["startupHidden"] = LoadedFromStartup && !IsVisible && tray != null; ShowSettings();
            foreach (var box in shortcutBoxes) box.Text = ""; shortcutBoxes[1].Text = "Ctrl+Alt+F10"; ApplyShortcuts(this, new());
            if (!ShortcutStatus.Text.Contains("Ctrl+Alt+F10")) throw new InvalidOperationException("Custom shortcut was not active.");
            NativeDesktop.SendChord([0x11, 0x12], 0x79);
            await Wait(() => SelectionOverlay.IsOpen, "Packaged custom shortcut dispatch"); checks["customShortcutOverlay"] = true;
            foreach (var overlay in Application.Current.Windows.OfType<Window>().Where(w => w != this).ToArray())
            {
                // Esc on one monitor cancels the whole selection. Other snapshot
                // entries may already be closed by the time they are visited.
                var source = PresentationSource.FromVisual(overlay);
                if (source != null) overlay.RaiseEvent(new KeyEventArgs(Keyboard.PrimaryDevice, source, 0, Key.Escape) { RoutedEvent = Keyboard.PreviewKeyDownEvent });
            }
            await Wait(() => !SelectionOverlay.IsOpen, "Snippet cancellation"); ShowSettings();
            var targetWindow = new Window { Title = "Game Text Reader capture verification", Owner = this, Width = 760, Height = 180, WindowStartupLocation = WindowStartupLocation.CenterOwner, Topmost = true, Background = Brushes.White, Content = new System.Windows.Controls.TextBlock { Text = "Native capture verification\nSentinel 27 saw an Angel!", Foreground = Brushes.Black, FontSize = 30, Margin = new Thickness(20) } };
            try
            {
                targetWindow.Show(); await Task.Delay(250); var targetHandle = new System.Windows.Interop.WindowInteropHelper(targetWindow).Handle;
                if (!NativeDesktop.GetWindowRect(targetHandle, out var bounds)) throw new InvalidOperationException("The capture test window has no bounds.");
                var captureSettings = settings.Clone(); captureSettings.Ocr.Enabled = false;
                var captured = await capture.ReadAsync([bounds.Left, bounds.Top, bounds.Right, bounds.Bottom], captureSettings, true, CancellationToken.None);
                if (!captured.Recognition.Text.Contains("Sentinel") || !captured.Recognition.Text.Contains("Angel")) throw new InvalidOperationException("Packaged screen capture/OCR did not recognize the test window.");
                checks["screenCaptureOcr"] = true; checks["captureMs"] = captured.CaptureMs + captured.Recognition.ElapsedMs;
            }
            finally { targetWindow.Close(); }
            const string sample = "# Reader workspace\n\nThe C# Reader keeps the source editable and follows each spoken word.\n\n1. **Observe:** Notice repeated words repeated words.\n2. **Listen:** Read comfortably.\n\n- **Evidence:** Keep words and their source positions aligned.\n\n$$E=mc^2$$\n\n<details><summary>More</summary>Extra information stays available.</details>\n\n![Diagram](https://example.com/blocked.png)";
            settings.Volume = 0; Persist(); SetText(sample, true); await Wait(() => previewReady && renderedRevision == revision, "Markdown preview");
            var headings = await Preview.ExecuteScriptAsync("document.querySelectorAll('h1').length"); var images = await Preview.ExecuteScriptAsync("document.querySelectorAll('img').length"); if (headings != "1" || images != "0") throw new InvalidOperationException("The packaged preview did not render safely."); checks["markdownPreview"] = true; checks["remoteImagesBlocked"] = true;
            StartReader(false); var id = currentRequest;
            async Task WaitSpeech(Func<SpeechDiagnostic, bool> predicate, string operation)
            {
                var start = Environment.TickCount64; while (Environment.TickCount64 - start < 30000) { if ((await speech.DiagnosticsAsync()).Any(s => s.Id == id && predicate(s))) return; await Task.Delay(50); }
                throw new InvalidOperationException(operation + " timed out.");
            }
            await WaitSpeech(s => s.Ready && s.HasTiming, "Muted Reader speech"); speech.Pause(id, sample, true); await WaitSpeech(s => s.Paused, "Reader pause");
            var doc = PrepareDocument(); int target = doc.Words.Count / 2; await Preview.ExecuteScriptAsync($"document.querySelector('[data-reader-word-index=\"{target}\"]').dispatchEvent(new MouseEvent('dblclick',{{bubbles:true}}));");
            await WaitSpeech(s => s.SourceOffset >= doc.Words[target].SourceStart && s.Paused, "Preview double-click seeking"); if ((await speech.DiagnosticsAsync()).Count(s => s.HasPlayer) != 1) throw new InvalidOperationException("More than one Reader player survived seeking."); checks["previewSeekSinglePlayer"] = true;
            ClearReader(); await Wait(() => Editor.IsKeyboardFocusWithin, "Edit focus after Clear"); Editor.TextArea.PerformTextInput("Typed after Clear"); if (Editor.Text != "Typed after Clear") throw new InvalidOperationException("The cleared editor did not accept typing."); checks["clearThenType"] = true;
            SetText(sample, true); await Wait(() => renderedRevision == revision, "Preview refresh"); if (dark) ThemeToggle.RaiseEvent(new RoutedEventArgs(System.Windows.Controls.Button.ClickEvent)); await Wait(() => !dark, "Light appearance"); ThemeToggle.RaiseEvent(new RoutedEventArgs(System.Windows.Controls.Button.ClickEvent)); await Wait(() => dark, "Dark appearance");
            var normalWidth = Width; Width = 800; UpdateLayout(); if (CaptureSidebar.Visibility != Visibility.Collapsed) throw new InvalidOperationException("The narrow layout did not collapse capture controls."); Width = Math.Max(normalWidth, 1280); UpdateLayout(); checks["responsiveWorkspace"] = true;
            var image = new RenderTargetBitmap((int)ActualWidth, (int)ActualHeight, 96, 96, PixelFormats.Pbgra32); image.Render(this); var encoder = new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(image)); Directory.CreateDirectory(Path.GetDirectoryName(reportPath)!); using (var file = File.Create(Path.ChangeExtension(reportPath, ".png"))) encoder.Save(file);
            checks["monitorCount"] = NativeDesktop.Monitors().Count; checks["passed"] = true;
        }
        catch (Exception e) { checks["passed"] = false; checks["error"] = e.ToString(); Environment.ExitCode = 1; }
        finally { Directory.CreateDirectory(Path.GetDirectoryName(reportPath)!); File.WriteAllText(reportPath, JsonSerializer.Serialize(checks, new JsonSerializerOptions { WriteIndented = true })); Quit(); }
    }
}
