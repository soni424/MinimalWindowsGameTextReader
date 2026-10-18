using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using GameTextReader.App;
using GameTextReader.Core;
using GameTextReader.Windows;
using ICSharpCode.AvalonEdit;
using Microsoft.Web.WebView2.Wpf;
using NUnit.Framework;

namespace GameTextReader.Tests;

[NonParallelizable, Apartment(ApartmentState.STA), SingleThreaded]
public class UiTests
{
    private static void EnsureApplication()
    {
        // WPF queues OnStartup even without Run; load resources without launching
        // the production AppData/integration path when the dispatcher is pumped.
        if (Application.Current == null) { var app = new GameTextReader.App.App(false); app.InitializeComponent(); }
        SynchronizationContext.SetSynchronizationContext(new DispatcherSynchronizationContext(Dispatcher.CurrentDispatcher));
    }
    internal static void PumpUntil(Func<bool> complete, int milliseconds = 30000)
    {
        var until = Environment.TickCount64 + milliseconds;
        while (!complete() && Environment.TickCount64 < until) { var frame = new DispatcherFrame(); var timer = new DispatcherTimer(DispatcherPriority.Background) { Interval = TimeSpan.FromMilliseconds(10) }; timer.Tick += (_, _) => { timer.Stop(); frame.Continue = false; }; timer.Start(); Dispatcher.PushFrame(frame); }
        Assert.That(complete(), Is.True, "UI operation did not finish within the timeout.");
    }
    private static void Close(MainWindow window) { typeof(MainWindow).GetField("quitting", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(window, true); window.Close(); }
    private static void CloseFixture(MainWindow window, string folder)
    {
        Close(window);
        for (int attempt = 0; attempt < 50; attempt++)
        {
            try { Directory.Delete(folder, true); return; }
            catch (IOException) { var wait = Task.Delay(100); PumpUntil(() => wait.IsCompleted); }
        }
    }
    private static string Script(WebView2CompositionControl preview, string javascript) { var task = preview.ExecuteScriptAsync(javascript); PumpUntil(() => task.IsCompleted); return task.GetAwaiter().GetResult(); }
    [Test]
    public void ThemeControlsVoiceNamesAndResponsiveCardsStayConsistent()
    {
        EnsureApplication(); var folder = Path.Combine(Path.GetTempPath(), "gtr-theme-test-" + Guid.NewGuid()); Directory.CreateDirectory(folder);
        var path = Path.Combine(folder, "config.json"); var store = new ConfigStore(path); var settings = store.Load(); settings.Theme = "dark"; store.Save(settings);
        var window = new MainWindow(path, true) { Width = 1500, Height = 1000 }; window.Show();
        try
        {
            var theme = window.FindName("ThemeToggle") as Button;
            Assert.That(theme, Is.Not.Null, "Appearance must be one sun/moon button, not a native dropdown.");
            Assert.That(System.Windows.Automation.AutomationProperties.GetName(theme!), Does.Contain("Light"));
            if (OperatingSystem.IsWindowsVersionAtLeast(10, 0, 22000))
            {
                const BindingFlags privateFields = BindingFlags.Instance | BindingFlags.NonPublic;
                Assert.That(typeof(MainWindow).GetField("captionThemeResult", privateFields)!.GetValue(window), Is.EqualTo(0), "The initial dark caption must be applied after HWND creation.");
                Assert.That(typeof(MainWindow).GetField("captionColorResult", privateFields)!.GetValue(window), Is.EqualTo(0), "Windows 11 caption colors must be set successfully.");
            }
            Assert.That(window.FindName("ExternalQuickActions"), Is.Null, "Switching tabs must not insert a shifting action strip.");
            var tabs = (TabControl)window.FindName("MainTabs"); var before = tabs.TranslatePoint(new Point(), window).Y; tabs.SelectedIndex = 1; window.UpdateLayout();
            Assert.That(tabs.TranslatePoint(new Point(), window).Y, Is.EqualTo(before).Within(.1)); tabs.SelectedIndex = 2; window.UpdateLayout();
            var voice = (FrameworkElement)window.FindName("VoiceCard"); var shortcuts = (FrameworkElement)window.FindName("ShortcutCard");
            Assert.That(voice.ActualWidth, Is.EqualTo(shortcuts.ActualWidth).Within(1));
            Assert.That(voice.ActualWidth + shortcuts.ActualWidth, Is.GreaterThan(window.ActualWidth - 130), "Wide settings cards must use the available width.");
            var selector = (ComboBox)window.FindName("Voices"); selector.ItemsSource = new[] { new VoiceInfo("test", "Readable voice", "sapi") }; selector.SelectedIndex = 0; window.UpdateLayout();
            Assert.That(VisualChildren<TextBlock>(selector).Any(t => t.Text == "Readable voice"), Is.True, "Selected voice must honor DisplayMemberPath.");
            var shortcutGrid = (Grid)window.FindName("ShortcutGrid"); PumpUntil(() => shortcutGrid.Children.OfType<Button>().All(b => b.Content is StackPanel)); Assert.That(shortcutGrid.Children.OfType<Button>().All(b => b.ActualHeight <= 40), Is.True, "Record/Clear must align with fields, not stretch across labels.");
            AssertPaletteContrast(window); SaveScreenshot(window, "voice-dark.png");
            theme!.RaiseEvent(new RoutedEventArgs(Button.ClickEvent)); Assert.That(new ConfigStore(path).Load().Theme, Is.EqualTo("light"));
            AssertPaletteContrast(window); window.UpdateLayout(); SaveScreenshot(window, "voice-light.png");
            window.Width = 850; window.UpdateLayout(); Assert.That(Grid.GetRow(shortcuts), Is.GreaterThan(Grid.GetRow(voice))); Assert.That(Grid.GetColumn(shortcuts), Is.Zero);
            SaveScreenshot(window, "voice-narrow.png");
        }
        finally { CloseFixture(window, folder); }
    }
    private static IEnumerable<T> VisualChildren<T>(DependencyObject parent) where T : DependencyObject
    {
        for (var i = 0; i < VisualTreeHelper.GetChildrenCount(parent); i++) { var child = VisualTreeHelper.GetChild(parent, i); if (child is T match) yield return match; foreach (var descendant in VisualChildren<T>(child)) yield return descendant; }
    }
    private static void AssertPaletteContrast(MainWindow window)
    {
        static double Luminance(Color color) { static double Linear(byte value) { var v = value / 255d; return v <= .04045 ? v / 12.92 : Math.Pow((v + .055) / 1.055, 2.4); } return .2126 * Linear(color.R) + .7152 * Linear(color.G) + .0722 * Linear(color.B); }
        var text = Luminance(((SolidColorBrush)window.FindResource("TextBrush")).Color);
        foreach (var key in new[] { "ControlBrush", "SelectedBrush", "HoverBrush", "EditorBrush" }) { var background = Luminance(((SolidColorBrush)window.FindResource(key)).Color); Assert.That((Math.Max(text, background) + .05) / (Math.Min(text, background) + .05), Is.GreaterThanOrEqualTo(4.5), key + " must retain readable text."); }
    }
    private static void SaveScreenshot(MainWindow window, string name)
    {
        window.UpdateLayout(); var image = new RenderTargetBitmap((int)window.ActualWidth, (int)window.ActualHeight, 96, 96, PixelFormats.Pbgra32); image.Render(window); var png = new PngBitmapEncoder(); png.Frames.Add(BitmapFrame.Create(image)); var folder = Path.GetFullPath(Path.Combine(TestContext.CurrentContext.WorkDirectory, "ui-artifacts")); Directory.CreateDirectory(folder); using var stream = File.Create(Path.Combine(folder, name)); png.Save(stream);
    }
    [Test]
    public void ProgressCoalescesAndPreviewRestoresOnlyCurrentDocumentWord()
    {
        EnsureApplication(); var folder = Path.Combine(Path.GetTempPath(), "gtr-progress-test-" + Guid.NewGuid()); Directory.CreateDirectory(folder);
        var window = new MainWindow(Path.Combine(folder, "config.json"), true); window.Show();
        try
        {
            const string source = "# Science\n\nRepeated words and repeated evidence remain aligned.\n\nAnother sentence follows.";
            window.SetText(source, true); var preview = (WebView2CompositionControl)window.FindName("Preview");
            const BindingFlags fields = BindingFlags.Instance | BindingFlags.NonPublic;
            PumpUntil(() => (long)typeof(MainWindow).GetField("renderedRevision", fields)!.GetValue(window)! >= 0);
            var document = SpeechText.Prepare(source, true); typeof(MainWindow).GetField("currentRequest", fields)!.SetValue(window, 77L); typeof(MainWindow).GetField("hasTiming", fields)!.SetValue(window, true);
            typeof(MainWindow).GetMethod("UpdateTransport", fields)!.Invoke(window, null); var playContent = ((Button)window.FindName("PlayButton")).Content;
            var queue = typeof(MainWindow).GetMethod("QueueProgress", fields)!;
            for (var i = 0; i < 50; i++) { var index = i % 5 + 1; var word = document.Words[index]; queue.Invoke(window, [new SpeechProgress(77, source, word.SourceStart, word.SourceEnd, index, 5)]); }
            const int expectedIndex = 5; var expected = document.Words[expectedIndex];
            PumpUntil(() => Script(preview, "document.querySelector('.speech-word')?.dataset.readerWordIndex") == "\"" + expectedIndex + "\"");
            Assert.That(((Button)window.FindName("PlayButton")).Content, Is.SameAs(playContent), "Word progress must not rebuild toolbar visuals.");
            Assert.That(Script(preview, "window.readerHighlight(0,-1,77,5,9999)"), Is.EqualTo("false"));
            Assert.That(Script(preview, "window.readerHighlight(0,Number.MAX_SAFE_INTEGER,77,5,9999)"), Is.EqualTo("false"));
            var beforeTheme = Script(preview, "document.body.className"); ((Button)window.FindName("ThemeToggle")).RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            PumpUntil(() => Script(preview, "document.body.className") != beforeTheme && Script(preview, "document.querySelector('.speech-word')?.dataset.readerWordIndex") == "\"" + expectedIndex + "\"");
            var stale = document.Words[0]; queue.Invoke(window, [new SpeechProgress(77, source, stale.SourceStart, stale.SourceEnd, 0, 4)]);
            var wait = Task.Delay(40); PumpUntil(() => wait.IsCompleted); Assert.That((int)typeof(MainWindow).GetField("currentOffset", fields)!.GetValue(window)!, Is.EqualTo(expected.SourceStart));
            window.SetText("# A new document\n\nNew words.", true); PumpUntil(() => Script(preview, "document.querySelector('h1')?.textContent") == "\"A new document\"");
            Assert.That(Script(preview, "document.querySelector('.speech-word') === null"), Is.EqualTo("true"));
        }
        finally { CloseFixture(window, folder); }
    }
    [Test]
    public void LatestPasteWinsWhilePreviewEnvironmentIsStillLoading()
    {
        EnsureApplication(); var folder = Path.Combine(Path.GetTempPath(), "gtr-paste-test-" + Guid.NewGuid()); Directory.CreateDirectory(folder);
        var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously); var requested = false;
        var desktop = new DesktopServices { CreatePreviewEnvironment = async path => { requested = true; await release.Task; return await Microsoft.Web.WebView2.Core.CoreWebView2Environment.CreateAsync(null, path); } };
        var window = new MainWindow(Path.Combine(folder, "config.json"), true, desktop); window.Show();
        try
        {
            window.SetText("# First paste\n\nThe preview is deliberately delayed.", true); PumpUntil(() => requested);
            for (var i = 0; i < 10; i++) window.SetText("# Superseded " + i + "\n\nObsolete text.", true);
            const string latest = "# Latest paste\n\nOnly this source should be displayed or spoken."; window.SetText(latest, true); release.TrySetResult();
            var preview = (WebView2CompositionControl)window.FindName("Preview");
            PumpUntil(() => preview.CoreWebView2 != null && (long)typeof(MainWindow).GetField("renderedRevision", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(window)! >= 0);
            PumpUntil(() => Script(preview, "document.querySelector('h1')?.textContent") == "\"Latest paste\"");
            Assert.That(window.ReaderText, Is.EqualTo(latest)); var prepared = (SpeechDocument)typeof(MainWindow).GetMethod("PrepareDocument", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(window, null)!;
            Assert.That(prepared.SourceText, Is.EqualTo(latest)); Assert.That(Script(preview, "document.body.textContent"), Does.Not.Contain("Superseded"));
        }
        finally { release.TrySetResult(); CloseFixture(window, folder); }
    }
    [Test]
    public void PlainCaptureProgressUsesExactSourceSpanAfterSwitchingToPreview()
    {
        EnsureApplication(); var folder = Path.Combine(Path.GetTempPath(), "gtr-span-test-" + Guid.NewGuid()); Directory.CreateDirectory(folder);
        var window = new MainWindow(Path.Combine(folder, "config.json"), true); window.Show();
        try
        {
            const string source = "Text [shown](https://example.com/path) continues here."; window.SetText(source, false);
            var plain = SpeechText.Prepare(source, false); var markdown = SpeechText.Prepare(source, true);
            int sourceStart = source.IndexOf("continues", StringComparison.Ordinal); int plainIndex = plain.Words.ToList().FindIndex(w => w.SourceStart == sourceStart); int previewIndex = markdown.Words.ToList().FindIndex(w => w.SourceStart == sourceStart);
            Assert.That(plainIndex, Is.Not.EqualTo(previewIndex), "This fixture must exercise different speech/Preview indices.");
            const BindingFlags fields = BindingFlags.Instance | BindingFlags.NonPublic;
            typeof(MainWindow).GetMethod("PrepareDocument", fields)!.Invoke(window, null); typeof(MainWindow).GetField("currentRequest", fields)!.SetValue(window, 55L);
            var word = plain.Words[plainIndex]; typeof(MainWindow).GetMethod("ShowProgress", fields)!.Invoke(window, [new SpeechProgress(55, source, word.SourceStart, word.SourceEnd, plainIndex, 1)]);
            ((TabControl)window.FindName("DocumentTabs")).SelectedIndex = 1; var preview = (WebView2CompositionControl)window.FindName("Preview");
            PumpUntil(() => preview.CoreWebView2 != null && (long)typeof(MainWindow).GetField("renderedRevision", fields)!.GetValue(window)! >= 0);
            PumpUntil(() => Script(preview, "document.querySelector('.speech-word')?.textContent") == "\"continues\"");
            Assert.That(Script(preview, "document.querySelector('.speech-word')?.dataset.readerWordIndex"), Is.EqualTo("\"" + previewIndex + "\""));
            Assert.That(window.ReaderText, Is.EqualTo(source));
        }
        finally { CloseFixture(window, folder); }
    }
    [Test]
    public void ReaderEditClearPreviewThemeAndResponsiveWorkspace()
    {
        EnsureApplication(); var folder = Path.Combine(Path.GetTempPath(), "gtr-ui-test-" + Guid.NewGuid()); Directory.CreateDirectory(folder); var window = new MainWindow(Path.Combine(folder, "config.json"), true) { Left = 40, Top = 40, Width = 1400, Height = 900 }; window.Show(); window.Activate();
        try
        {
            var editor = (TextEditor)window.FindName("Editor"); var tabs = (TabControl)window.FindName("DocumentTabs"); var preview = (WebView2CompositionControl)window.FindName("Preview");
            window.SetText("# How Science Works\n\n1. **Observation:** Notice repeated words repeated words.\n2. **Question:** Why?\n\n- **Evidence:** Check it.\n\n$$x^2+y^2$$\n\n![A diagram](https://example.com/image.png)\n\n<details><summary>More</summary>Extra prose.</details>", true);
            PumpUntil(() => preview.CoreWebView2 != null && (long)typeof(MainWindow).GetField("renderedRevision", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(window)! >= 0);
            Assert.That(Script(preview, "document.querySelector('h1').textContent"), Does.Contain("How Science Works"));
            Assert.That(Script(preview, "document.querySelectorAll('[data-reader-word-index]').length"), Is.Not.EqualTo("0")); Assert.That(Script(preview, "document.querySelectorAll('img').length"), Is.EqualTo("0"));
            Assert.That(Script(preview, "Math.abs(document.querySelector('.ordered-marker').getBoundingClientRect().top - document.querySelector('li strong').getBoundingClientRect().top) < 5"), Is.EqualTo("true"), "Ordered markers must stay on the same rendered line as the item.");
            var selectedIds = Script(preview, "JSON.stringify(Array.from(document.querySelectorAll('[data-reader-word-index]')).filter(e=>e.textContent==='repeated').map(e=>e.dataset.readerWordIndex))"); var ids = JsonSerializer.Deserialize<string>(selectedIds)!; Assert.That(JsonSerializer.Deserialize<string[]>(ids)!.Distinct().Count(), Is.EqualTo(2));
            var theme = (Button)window.FindName("ThemeToggle"); var initialTheme = Script(preview, "document.body.className"); if (initialTheme == "\"dark\"") theme.RaiseEvent(new RoutedEventArgs(Button.ClickEvent)); PumpUntil(() => Script(preview, "document.body.className") == "\"light\""); theme.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            window.Width = 800; window.UpdateLayout(); Assert.That(((FrameworkElement)window.FindName("CaptureSidebar")).Visibility, Is.EqualTo(Visibility.Collapsed)); Assert.That(((FrameworkElement)window.FindName("CaptureControlsButton")).Visibility, Is.EqualTo(Visibility.Visible));
            const BindingFlags fields = BindingFlags.Instance | BindingFlags.NonPublic;
            typeof(MainWindow).GetField("currentRequest", fields)!.SetValue(window, 11L); typeof(MainWindow).GetField("playbackRevision", fields)!.SetValue(window, 20L); typeof(MainWindow).GetField("rateRevision", fields)!.SetValue(window, 100L);
            typeof(MainWindow).GetMethod("ShowProgress", fields)!.Invoke(window, [new SpeechProgress(11, editor.Text, 3, 6, 0, 19)]); Assert.That(typeof(MainWindow).GetField("currentOffset", fields)!.GetValue(window), Is.EqualTo(0));
            window.AcceptRateResult(new(11, 99, 1, .5, "old rejection")); Assert.That(((Slider)window.FindName("ReaderSpeed")).Value, Is.EqualTo(1));
            window.ClearReader(); PumpUntil(() => editor.IsKeyboardFocusWithin); Assert.That(tabs.SelectedIndex, Is.Zero); Assert.That(preview.Visibility, Is.EqualTo(Visibility.Collapsed)); editor.TextArea.PerformTextInput("Typed after Clear"); Assert.That(editor.Text, Is.EqualTo("Typed after Clear"));
            var clipboard = Clipboard.GetDataObject(); try { Clipboard.SetText(" and pasted"); editor.CaretOffset = editor.Text.Length; editor.Paste(); Assert.That(editor.Text, Does.EndWith(" and pasted")); } finally { if (clipboard != null) Clipboard.SetDataObject(clipboard, true); }
            window.Width = 1400; window.SetText("# Reader workspace\n\nThe C# Reader keeps the source editable.\n\n1. **Observe:** Notice details.\n2. **Listen:** Read comfortably.\n\n- **Evidence:** Keep the words and their source positions aligned.", true); PumpUntil(() => tabs.SelectedIndex == 1 && Script(preview, "document.querySelector('h1')?.textContent") == "\"Reader workspace\"");
            var image = new RenderTargetBitmap((int)window.ActualWidth, (int)window.ActualHeight, 96, 96, PixelFormats.Pbgra32); image.Render(window); var png = new PngBitmapEncoder(); png.Frames.Add(BitmapFrame.Create(image)); var artifacts = Path.GetFullPath(Path.Combine(TestContext.CurrentContext.WorkDirectory, "ui-artifacts")); Directory.CreateDirectory(artifacts); using var stream = File.Create(Path.Combine(artifacts, "reader-dark.png")); png.Save(stream); TestContext.Out.WriteLine("UI screenshot: " + artifacts);
        }
        finally { Close(window); for (int attempt = 0; attempt < 50; attempt++) { try { Directory.Delete(folder, true); break; } catch (IOException) { var wait = Task.Delay(100); PumpUntil(() => wait.IsCompleted); } } }
    }
    [TestCase(false)]
    [TestCase(true)]
    public void MissingPreviewAssetsReportFailureAndKeepEditUsable(bool missingScriptOnly)
    {
        EnsureApplication(); var folder = Path.Combine(Path.GetTempPath(), "gtr-preview-test-" + Guid.NewGuid()); Directory.CreateDirectory(folder);
        if (missingScriptOnly) File.Copy(Path.Combine(AppContext.BaseDirectory, "Assets", "Preview", "index.html"), Path.Combine(folder, "index.html"));
        var window = new MainWindow(Path.Combine(folder, "config.json"), true, new DesktopServices { PreviewAssetDirectory = folder }); window.Show();
        try
        {
            var error = (TextBlock)window.FindName("PreviewError");
            window.SetText("# Missing preview assets\n\nThe source must remain usable.", true); PumpUntil(() => error.Text.Length > 0);
            Assert.That(error.Text, Does.Contain("Preview is unavailable"), "A failed local page must not be presented as a working Preview.");
            window.ClearReader(); var editor = (TextEditor)window.FindName("Editor"); PumpUntil(() => editor.IsKeyboardFocusWithin); editor.TextArea.PerformTextInput("Still editable"); Assert.That(window.ReaderText, Is.EqualTo("Still editable"));
        }
        finally { CloseFixture(window, folder); }
    }
    [Test]
    public void MissingWebViewRuntimeStillAllowsClearPasteAndPlayback()
    {
        EnsureApplication(); var folder = Path.Combine(Path.GetTempPath(), "gtr-runtime-test-" + Guid.NewGuid()); Directory.CreateDirectory(folder);
        var path = Path.Combine(folder, "config.json"); var store = new ConfigStore(path); var settings = store.Load(); settings.Volume = 0; store.Save(settings);
        var services = new DesktopServices { CreatePreviewEnvironment = _ => Task.FromException<Microsoft.Web.WebView2.Core.CoreWebView2Environment>(new Microsoft.Web.WebView2.Core.WebView2RuntimeNotFoundException("Simulated missing WebView2 Runtime")) };
        var window = new MainWindow(path, true, services); window.Show(); window.Activate();
        try
        {
            window.SetText("# A heading\n\nText remains available without Preview.", true);
            var error = (TextBlock)window.FindName("PreviewError"); PumpUntil(() => error.Text.Contains("Preview is unavailable"));
            Assert.That(error.Text, Does.Contain("WebView2"));
            window.ClearReader(); var editor = (TextEditor)window.FindName("Editor"); PumpUntil(() => editor.IsKeyboardFocusWithin);
            editor.TextArea.PerformTextInput("Typed "); var previous = Clipboard.GetDataObject();
            try { Clipboard.SetText("and pasted after Clear."); editor.Paste(); } finally { if (previous != null) Clipboard.SetDataObject(previous, true); else Clipboard.Clear(); }
            Assert.That(window.ReaderText, Is.EqualTo("Typed and pasted after Clear."));
            ((Button)window.FindName("PlayButton")).RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            PumpUntil(() => ((TextBlock)window.FindName("StatusText")).Text.Contains("Reading aloud"));
        }
        finally { CloseFixture(window, folder); }
    }
    [Test]
    public void TrayFailureMakesSignInLaunchVisibleWithoutChangingStartupPreference()
    {
        EnsureApplication(); var folder = Path.Combine(Path.GetTempPath(), "gtr-tray-test-" + Guid.NewGuid()); Directory.CreateDirectory(folder);
        var path = Path.Combine(folder, "config.json"); var store = new ConfigStore(path); var settings = store.Load();
        settings.Startup.Enabled = false; settings.Hotkeys = new() { Fixed = "", Snippet = "", ReadAgain = "", AutoRead = "" }; store.Save(settings);
        var window = new MainWindow(path, false, new DesktopServices { CreateTrayIcon = () => throw new IOException("Simulated unavailable notification area") });
        try
        {
            Assert.That(window.IsVisible, Is.False); window.InitializeServices(true);
            PumpUntil(() => window.IsVisible && ((ComboBox)window.FindName("Voices")).Items.Count > 0);
            Assert.That(((TextBlock)window.FindName("StatusText")).Text, Does.Contain("Tray unavailable"));
            Assert.That(new ConfigStore(path).Load().Startup.Enabled, Is.False);
        }
        finally { CloseFixture(window, folder); }
    }
    [Test]
    public void ActualCustomHotkeyDispatchOpensSnippetOverlayAndUnregistersOldKey()
    {
        EnsureApplication(); var window = new Window { Width = 320, Height = 160, Title = "Game Text Reader shortcut verification" }; window.Show(); window.Activate(); PumpUntil(() => window.IsActive); var hit = 0; using var keys = new HotkeyRegistration(); Task<int[]?>? selection = null;
        var log = new System.Collections.Concurrent.ConcurrentQueue<string>(); keys.Log += message => log.Enqueue(message);
        keys.Fired += index => window.Dispatcher.BeginInvoke(() => { Assert.That(index, Is.EqualTo(1)); hit++; selection = SelectionOverlay.SelectAsync(null, false); });
        try
        {
            keys.Apply(["", "Ctrl+Alt+F10", "", ""]); NativeDesktop.SendChord([0x11, 0x12], 0x79);
            try { PumpUntil(() => SelectionOverlay.IsOpen); } catch (AssertionException) { Assert.Fail("Native shortcut/overlay failed. UI callbacks: " + hit + "; " + string.Join("; ", log)); }
            TestContext.Out.WriteLine("Overlay opened; " + string.Join("; ", log));
            Assert.That(hit, Is.EqualTo(1)); var overlay = Application.Current.Windows.OfType<Window>().First(w => w != window);
            TestContext.Out.WriteLine("Canceling overlay: " + overlay.Title + "; style=" + overlay.WindowStyle);
            overlay.RaiseEvent(new KeyEventArgs(Keyboard.PrimaryDevice, PresentationSource.FromVisual(overlay), 0, Key.Escape) { RoutedEvent = Keyboard.PreviewKeyDownEvent });
            PumpUntil(() => !SelectionOverlay.IsOpen); Assert.That(selection!.IsCompleted, Is.True);
            keys.Apply(["", "Ctrl+Alt+F9", "", ""]); using var other = new HotkeyRegistration(); Assert.DoesNotThrow(() => other.Apply(["Ctrl+Alt+F10", "", "", ""]));
            var monitor = NativeDesktop.Monitors()[0]; var box = new[] { monitor.Left + 100, monitor.Top + 200, monitor.Left + 500, monitor.Top + 350 };
            var edited = SelectionOverlay.SelectAsync(box, true); var pane = Application.Current.Windows.OfType<Window>().First(w => w.WindowStyle == WindowStyle.None); var canvas = (Canvas)pane.Content; var panel = canvas.Children.OfType<Border>().Single();
            Assert.That(Canvas.GetLeft(panel) + panel.Width / 2, Is.EqualTo(canvas.ActualWidth / 2).Within(2), "The panel must be centered immediately on opening."); Assert.That(canvas.Children.OfType<System.Windows.Shapes.Rectangle>().Count(), Is.GreaterThan(1));
            pane.RaiseEvent(new KeyEventArgs(Keyboard.PrimaryDevice, PresentationSource.FromVisual(pane), 0, Key.Right) { RoutedEvent = Keyboard.PreviewKeyDownEvent });
            pane.RaiseEvent(new KeyEventArgs(Keyboard.PrimaryDevice, PresentationSource.FromVisual(pane), 0, Key.Enter) { RoutedEvent = Keyboard.PreviewKeyDownEvent });
            Assert.That(edited.Result, Is.EqualTo(new[] { box[0] + 1, box[1], box[2] + 1, box[3] })); Assert.That(box[0], Is.EqualTo(monitor.Left + 100), "Editing must not mutate the saved input array.");
        }
        finally { foreach (var overlay in Application.Current.Windows.OfType<Window>().Where(w => w != window && PresentationSource.FromVisual(w) != null).ToArray()) overlay.RaiseEvent(new KeyEventArgs(Keyboard.PrimaryDevice, PresentationSource.FromVisual(overlay), 0, Key.Escape) { RoutedEvent = Keyboard.PreviewKeyDownEvent }); window.Close(); }
    }
}
