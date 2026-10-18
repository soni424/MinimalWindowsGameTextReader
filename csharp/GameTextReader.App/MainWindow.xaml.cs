using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using GameTextReader.Core;
using GameTextReader.Windows;
using ICSharpCode.AvalonEdit.Document;
using Microsoft.Web.WebView2.Core;

namespace GameTextReader.App;

public partial class MainWindow : Window
{
    private readonly ConfigStore store; private AppSettings settings; private readonly bool noIntegrations;
    private readonly DesktopServices desktop;
    private readonly SpeechService speech = new(); private readonly OcrCorrector corrector = new(); private readonly CaptureService capture; private readonly AutoReadWatcher auto;
    private HotkeyRegistration? hotkeys; private System.Windows.Forms.NotifyIcon? tray;
    private bool initializing = true, programmatic, quitting, dark = true, sidebarExpanded, previewReady, previewLoading, imagesApproved, currentReplay;
    private SpeechDocument? prepared; private MarkdownPreview? rendered; private CorrectionResult? correction;
    private long revision, renderedRevision = -1, currentRequest, ownerHighWater, autoRequest, playbackRevision, rateRevision; private int currentOffset; private bool paused, hasTiming;
    private readonly HashSet<long> mapped = []; private CancellationTokenSource? manualCapture; private readonly ReadingMarker marker = new();
    private int[]? normalBounds; private bool restoringWindow;
    private readonly DispatcherTimer editDebounce = new() { Interval = TimeSpan.FromMilliseconds(180) }; private readonly DispatcherTimer speedDebounce = new() { Interval = TimeSpan.FromMilliseconds(120) };
    private readonly TextBox[] shortcutBoxes = new TextBox[4]; private bool markdownMode;
    private readonly Dictionary<string, ImageSource> icons = [];
    private readonly ConditionalWeakTable<Button, ButtonDecoration> decorations = new();
    private sealed record ButtonDecoration(string Text, string Icon, string Palette);
    private readonly object progressGate = new(); private SpeechProgress? pendingProgress; private bool progressQueued;
    private int highlightedWord = -1, transportSentence = -1;
    private CancellationTokenSource? preparation; private Task? preparationTask; private long preparationRevision = -1; private bool preparationMarkdown;
    private long preparedRevision = -1, renderedThemeRevision = -1, themeRevision; private bool preparedMarkdown, renderingPreview, previewRenderPending;
    private bool highlightScriptRunning, highlightScriptPending;
    private long highlightSequence;
    private int? captionThemeResult, captionColorResult;
    private SpeechDocument? previewIndexDocument;
    private readonly Dictionary<(int Start, int End), int> previewWordIndices = [];
    public bool LoadedFromStartup { get; set; }
    public string ReaderText => Editor.Text;
    public string SettingsPath => store.PathName;
    public MainWindow(string? settingsPath = null, bool noIntegrations = false) : this(settingsPath, noIntegrations, new DesktopServices()) { }
    internal MainWindow(string? settingsPath, bool noIntegrations, DesktopServices desktop)
    {
        this.desktop = desktop;
        this.noIntegrations = noIntegrations; store = new(settingsPath);
        try { settings = store.Load(settingsPath == null ? new[] { Path.Combine(AppContext.BaseDirectory, "config.json"), Path.Combine(AppContext.BaseDirectory, "_internal", "config.json") } : []); }
        catch (Exception e) { settings = store.Current.Clone(); MessageBox.Show("Settings could not be loaded. The existing file was preserved.\n" + e.Message, "Game Text Reader"); }
        capture = new(corrector); auto = new(capture, () => store.Current.Clone()); InitializeComponent();
        Editor.TextArea.TextView.BackgroundRenderers.Add(marker); Editor.TextArea.TextView.LineTransformers.Add(marker); BuildShortcutRows(); BuildContextMenu();
        if (settings.Theme == "system") settings.Theme = NativeDesktop.DarkSystemTheme() ? "dark" : "light";
        OcrMode.ItemsSource = new[] { "Standard", "Enhanced" }; AutoSpeed.ItemsSource = new[] { "Normal", "Fast", "Instant" }; CorrectionStrength.ItemsSource = new[] { "Conservative", "Balanced", "Strong" }; SpeechMode.ItemsSource = new[] { "Replace current line", "Queue next line", "Allow overlapping lines" }; MaxOverlap.ItemsSource = new[] { 2, 3, 4 };
        LoadSettingsUi(); ApplyTheme(); BuildLabel.Text = BuildInformation();
        editDebounce.Tick += async (_, _) => { editDebounce.Stop(); await PreparePreview(); };
        speedDebounce.Tick += (_, _) => { speedDebounce.Stop(); if (Persist() && currentRequest > 0 && currentReplay) rateRevision = speech.SetRate(currentRequest, Editor.Text, settings.Speech.ReaderPlaybackRate); };
        DataObject.AddPastingHandler(Editor, (_, e) => { if (e.DataObject.GetData(DataFormats.UnicodeText) is string text && SpeechText.LooksLikeMarkdown(text)) Dispatcher.BeginInvoke(() => { markdownMode = true; DocumentTabs.SelectedIndex = 1; }); });
        speech.Preparing += (id, source) => Ui(() => { if (mapped.Contains(id) && source == Editor.Text && id >= ownerHighWater) { currentRequest = id; ownerHighWater = id; SetStatus("Preparing speech…"); UpdateTransport(); } });
        speech.Started += (id, source) => Ui(() => { if (mapped.Contains(id) && source == Editor.Text && id >= ownerHighWater) { currentRequest = id; ownerHighWater = id; SetStatus("Reading aloud."); UpdateTransport(); } });
        speech.Finished += (id, _) => Ui(() => { mapped.Remove(id); if (currentRequest == id) { currentRequest = 0; paused = false; hasTiming = false; ClearHighlight(); UpdateTransport(); SetStatus("Reading finished."); } });
        speech.Progress += QueueProgress;
        speech.StateChanged += state => Ui(() => { if (state.RequestId == currentRequest && state.SourceText == Editor.Text && state.PlaybackRevision >= playbackRevision) { playbackRevision = state.PlaybackRevision; paused = state.Paused; hasTiming = state.HasTiming; currentOffset = state.SourceOffset; UpdateTransport(); } });
        speech.Error += message => Ui(() => SetStatus(message));
        speech.RateResult += result => Ui(() => AcceptRateResult(result));
        auto.Status += (message, _) => Ui(() => { AutoStatus.Text = message; AutoButton.Content = auto.Active ? "Stop Auto-Read" : "Start Auto-Read"; DecorateButton(AutoButton, auto.Active ? "stop" : "play"); });
        auto.CancelGrowth += (session, epoch) => Ui(() => { if (auto.IsCurrent(session, epoch) && autoRequest > 0) speech.Cancel(autoRequest); });
        auto.Line += (session, epoch, result) => Ui(() => { if (auto.IsCurrent(session, epoch)) AcceptCapture(result, true); });
        Loaded += (_, _) => { DecorateButtons(this); UpdateTransport(); };
        SourceInitialized += (_, _) => { RestorePhysicalPlacement(); ApplyCaptionTheme(); }; LocationChanged += (_, _) => RememberPhysicalPlacement();
        initializing = false; UpdateTransport();
    }
    public async void InitializeServices(bool startup)
    {
        try
        {
            var hwnd = new WindowInteropHelper(this).EnsureHandle();
            if (!noIntegrations)
            {
                hotkeys = new(); hotkeys.Log += DebugLog; hotkeys.Fired += index => Ui(() => DispatchShortcut(index)); HwndSource.FromHwnd(hwnd)?.AddHook(WindowMessage); ApplyActiveHotkeys(); CreateTray();
                if (settings.Startup.Enabled) try { new StartupRegistration().SetEnabled(true, Environment.ProcessPath!); } catch (Exception e) { SetStatus(e.Message); }
            }
            if (startup && tray == null) Show();
            var voices = await SpeechService.ListVoicesAsync(); initializing = true; Voices.ItemsSource = voices; var selected = voices.FirstOrDefault(v => v.Id == settings.Voice || v.Backend == "sapi" && settings.Voice.StartsWith("sapi:") && (v.Id[5..].EndsWith(settings.Voice[5..], StringComparison.OrdinalIgnoreCase) || settings.Voice[5..].EndsWith(v.Id[5..], StringComparison.OrdinalIgnoreCase))); Voices.SelectedItem = selected ?? (settings.Voice.Length == 0 ? voices.FirstOrDefault() : null); VoiceCount.Text = $"{voices.Count} installed Windows voices"; if (settings.Voice.Length > 0 && selected == null) VoiceCount.Text += " · saved voice unavailable; choose another"; initializing = false;
            if (string.IsNullOrEmpty(settings.Voice) && Voices.SelectedItem is VoiceInfo voice) { settings.Voice = voice.Id; Persist(); }
            await Task.Run(corrector.Preload);
        }
        catch (Exception e) { initializing = false; SetStatus("Initialization: " + e.Message); if (startup) Show(); }
    }
    private void LoadSettingsUi()
    {
        var previous = initializing; initializing = true;
        OcrMode.SelectedItem = TitleCase(settings.Ocr.RecognitionMode); AutoSpeed.SelectedItem = TitleCase(settings.AutoRead.Speed);
        CorrectionEnabled.IsChecked = settings.Ocr.Enabled; CorrectionStrength.SelectedItem = TitleCase(settings.Ocr.Strength); DebugLogging.IsChecked = settings.Ocr.DebugLogging;
        ProtectedTerms.Text = string.Join(Environment.NewLine, settings.Ocr.ProtectedWords); Rules.ItemsSource = settings.Ocr.Replacements.ToArray();
        ReaderSpeed.Value = settings.Speech.ReaderPlaybackRate; ReaderSpeedText.Text = settings.Speech.ReaderPlaybackRate.ToString("0.0") + "×"; BaseSpeed.Value = settings.Rate; Volume.Value = settings.Volume; BaseSpeedLabel.Text = "Base speed: " + settings.Rate; VolumeLabel.Text = "Volume: " + (int)settings.Volume;
        SpeechMode.SelectedIndex = settings.Speech.CaptureMode switch { "queue" => 1, "overlap" => 2, _ => 0 }; MaxOverlap.SelectedItem = settings.Speech.MaxOverlap; StartupEnabled.IsChecked = settings.Startup.Enabled;
        var keys = settings.Hotkeys.Values(); for (int i = 0; i < 4; i++) shortcutBoxes[i].Text = keys[i];
        RefreshProfiles(); var monitor = NativeDesktop.Monitors().FirstOrDefault(m => m.Contains(settings.Window.X ?? 0, settings.Window.Y ?? 0)) ?? NativeDesktop.Monitors().First(); Width = Math.Max(MinWidth, settings.Window.Width * 96d / monitor.DpiX); Height = Math.Max(MinHeight, settings.Window.Height * 96d / monitor.DpiY); initializing = previous;
    }
    private void RefreshProfiles() { var previous = initializing; initializing = true; Profiles.ItemsSource = settings.Profiles.ToArray(); Profiles.SelectedItem = settings.SelectedProfile; initializing = previous; UpdateArea(); }
    private void UpdateArea()
    {
        try { var b = CaptureGeometry.Resolve(settings.SelectedProfile.Area, NativeDesktop.Monitors()); AreaText.Text = $"{b[2] - b[0]} × {b[3] - b[1]} px\nPosition {b[0]}, {b[1]}"; }
        catch (Exception e) { AreaText.Text = e.Message; }
    }
    private static string TitleCase(string s) => s.Length == 0 ? s : char.ToUpperInvariant(s[0]) + s[1..];
    private bool Persist()
    {
        if (initializing) return false;
        try { store.Save(settings); return true; }
        catch (Exception e) { SetStatus("Settings were not saved: " + e.Message); settings = store.Current.Clone(); return false; }
    }
    private void Ui(Action action) { if (!Dispatcher.HasShutdownStarted) Dispatcher.BeginInvoke(action); }
    public void SetStatus(string text) { StatusText.Text = text; DebugLog(text); }
    private void DebugLog(string text)
    {
        if (!settings.Ocr.DebugLogging) return;
        try { var path = Path.Combine(Path.GetDirectoryName(store.PathName)!, "ocr_debug.log"); Directory.CreateDirectory(Path.GetDirectoryName(path)!); if (File.Exists(path) && new FileInfo(path).Length > 1_000_000) File.Move(path, path + ".1", true); File.AppendAllText(path, DateTime.UtcNow.ToString("O") + " " + text + Environment.NewLine); } catch (IOException) { }
    }
    private static string BuildInformation()
    {
        var version = Assembly.GetExecutingAssembly().GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion.Split('+')[0] ?? "unknown";
        var file = Path.Combine(AppContext.BaseDirectory, "build_info.json"); if (File.Exists(file)) try { using var info = JsonDocument.Parse(File.ReadAllText(file)); return "Version " + version + " · Build " + info.RootElement.GetProperty("build").GetString(); } catch (Exception) { }
        return "Version " + version + " · C# development build";
    }
    private void ApplyTheme()
    {
        dark = settings.Theme == "dark"; marker.Dark = dark;
        string[] keys = ["BackgroundBrush", "SurfaceBrush", "ControlBrush", "TextBrush", "MutedBrush", "BorderBrush", "EditorBrush", "HoverBrush", "SelectedBrush", "FocusBrush", "DisabledBrush"];
        string[] colors = dark ? ["#0D1420", "#152131", "#213249", "#E9F1FC", "#A8BDD7", "#536D8B", "#0E1927", "#304A69", "#214F7D", "#83BDFF", "#A0AFC1"] : ["#EDF2F8", "#FFFFFF", "#E1EAF5", "#172638", "#4F627C", "#6B829D", "#F9FBFE", "#D1E2F5", "#BDD8F5", "#005DBB", "#61748B"];
        for (int i = 0; i < keys.Length; i++) Application.Current.Resources[keys[i]] = new SolidColorBrush((Color)ColorConverter.ConvertFromString(colors[i]));
        ThemeToggle.Content = ThemeSymbol(dark); ThemeToggle.ToolTip = dark ? "Switch to Light appearance" : "Switch to Dark appearance"; System.Windows.Automation.AutomationProperties.SetName(ThemeToggle, (string)ThemeToggle.ToolTip);
        Editor.TextArea.TextView.InvalidateLayer(ICSharpCode.AvalonEdit.Rendering.KnownLayer.Selection); Editor.TextArea.TextView.Redraw(); DecorateButtons(this); themeRevision++; _ = RenderPreview(); ApplyCaptionTheme();
    }
    private FrameworkElement ThemeSymbol(bool currentDark)
    {
        var geometry = Geometry.Parse(currentDark ? "M 10 5 A 5 5 0 1 1 9.99 5 M 10 0 L 10 2 M 10 18 L 10 20 M 0 10 L 2 10 M 18 10 L 20 10 M 3 3 L 4.5 4.5 M 15.5 15.5 L 17 17 M 3 17 L 4.5 15.5 M 15.5 4.5 L 17 3" : "M 15 1 A 9 9 0 1 0 19 15 A 8 8 0 0 1 15 1"); geometry.Freeze();
        return new System.Windows.Shapes.Path { Data = geometry, Stroke = (Brush)FindResource("TextBrush"), StrokeThickness = 1.6, Width = 20, Height = 20, Stretch = Stretch.Uniform };
    }
    private void ApplyCaptionTheme()
    {
        var handle = new WindowInteropHelper(this).Handle; if (handle == 0) return;
        int value = dark ? 1 : 0; captionThemeResult = DwmSetWindowAttribute(handle, 20, ref value, sizeof(int)); if (captionThemeResult < 0) captionThemeResult = DwmSetWindowAttribute(handle, 19, ref value, sizeof(int));
        if (OperatingSystem.IsWindowsVersionAtLeast(10, 0, 22000))
        {
            var background = ((SolidColorBrush)FindResource("BackgroundBrush")).Color; var foreground = ((SolidColorBrush)FindResource("TextBrush")).Color;
            int caption = background.R | background.G << 8 | background.B << 16; int text = foreground.R | foreground.G << 8 | foreground.B << 16;
            captionColorResult = DwmSetWindowAttribute(handle, 35, ref caption, sizeof(int)); DwmSetWindowAttribute(handle, 36, ref text, sizeof(int));
        }
    }
    [System.Runtime.InteropServices.DllImport("dwmapi.dll")] private static extern int DwmSetWindowAttribute(nint hwnd, int attribute, ref int value, int size);
    private void ThemeChanged(object sender, RoutedEventArgs e) { if (initializing) return; settings.Theme = dark ? "light" : "dark"; if (Persist()) ApplyTheme(); }
    private void WindowResized(object sender, SizeChangedEventArgs e)
    {
        if (SidebarColumn == null) return; bool narrow = ActualWidth < 1100; CaptureControlsButton.Visibility = narrow ? Visibility.Visible : Visibility.Collapsed; var show = !narrow || sidebarExpanded; SidebarColumn.Width = new GridLength(show ? Math.Min(310, ActualWidth * .42) : 0); CaptureSidebar.Visibility = show ? Visibility.Visible : Visibility.Collapsed;
        if (VoiceWorkspace != null)
        {
            VoiceWorkspace.ColumnDefinitions[1].Width = narrow ? new GridLength(0) : new GridLength(1, GridUnitType.Star); Grid.SetRow(ShortcutCard, narrow ? 1 : 0); Grid.SetColumn(ShortcutCard, narrow ? 0 : 1); Grid.SetRow(UtilitiesCard, narrow ? 2 : 1); Grid.SetColumnSpan(UtilitiesCard, narrow ? 1 : 2);
            Grid.SetColumn(UtilityActions, narrow ? 0 : 1); Grid.SetRow(UtilityActions, narrow ? 1 : 0); Grid.SetColumnSpan(UtilityActions, narrow ? 2 : 1); UtilityActions.Margin = new Thickness(0, narrow ? 8 : 0, 0, 0); UtilityActions.HorizontalAlignment = narrow ? HorizontalAlignment.Left : HorizontalAlignment.Right;
        }
        RememberPhysicalPlacement();
    }
    private void ToggleSidebar(object sender, RoutedEventArgs e) { sidebarExpanded = !sidebarExpanded; WindowResized(this, null!); }
    private void MainTabChanged(object sender, SelectionChangedEventArgs e) { if (ReferenceEquals(e.Source, MainTabs)) Dispatcher.BeginInvoke(DispatcherPriority.Loaded, () => { if (!quitting) DecorateButtons(MainTabs); }); }
    private void AboutClick(object sender, RoutedEventArgs e) => Dialogs.Info(this, "About Game Text Reader", BuildInformation() + "\n\nApplication: " + Environment.ProcessPath + "\nSettings: " + store.PathName + "\n\nC#/.NET 10 · WPF · Windows OCR and speech");
    private void ProfileChanged(object sender, SelectionChangedEventArgs e) { if (initializing || Profiles.SelectedItem is not CaptureProfile profile) return; auto.Stop(); settings.SelectedProfileId = profile.Id; Persist(); UpdateArea(); }
    private void NewProfile(object sender, RoutedEventArgs e) { var name = Dialogs.Prompt(this, "New profile", "Profile name"); if (name == null) return; if (!CheckProfileName(name)) return; var profile = new CaptureProfile { Id = Guid.NewGuid().ToString("N"), Name = name.Trim() }; settings.Profiles.Add(profile); settings.SelectedProfileId = profile.Id; Persist(); RefreshProfiles(); }
    private bool CheckProfileName(string name, string? ignore = null) { if (string.IsNullOrWhiteSpace(name) || settings.Profiles.Any(p => p.Id != ignore && p.Name.Equals(name.Trim(), StringComparison.OrdinalIgnoreCase))) { SetStatus("Enter a unique, nonempty profile name."); return false; } return true; }
    private void RenameProfile(object sender, RoutedEventArgs e) { var p = settings.SelectedProfile; var name = Dialogs.Prompt(this, "Rename profile", "Profile name", p.Name); if (name == null || !CheckProfileName(name, p.Id)) return; p.Name = name.Trim(); Persist(); RefreshProfiles(); }
    private void DeleteProfile(object sender, RoutedEventArgs e) { if (settings.Profiles.Count <= 1) { SetStatus("At least one profile must remain."); return; } if (!Dialogs.Confirm(this, "Delete profile", "Delete “" + settings.SelectedProfile.Name + "”?")) return; auto.Stop(); settings.Profiles.Remove(settings.SelectedProfile); settings.SelectedProfileId = settings.Profiles[0].Id; Persist(); RefreshProfiles(); }
    private async void SetArea(object sender, RoutedEventArgs e)
    {
        auto.Stop(); int[]? initial = null; try { initial = CaptureGeometry.Resolve(settings.SelectedProfile.Area, NativeDesktop.Monitors()); } catch (InvalidOperationException) { }
        var restore = IsVisible && WindowState != WindowState.Minimized; if (restore) WindowState = WindowState.Minimized;
        var result = await SelectionOverlay.SelectAsync(initial, true); if (restore) ShowSettings(); if (result == null) return; settings.SelectedProfile.Area = CaptureGeometry.Build(result, NativeDesktop.Monitors()); Persist(); RefreshProfiles(); SetStatus("Capture area saved.");
    }
    private async void SelectSnippet(object sender, RoutedEventArgs e)
    {
        if (SelectionOverlay.IsOpen) return; if (IsVisible) WindowState = WindowState.Minimized; var result = await SelectionOverlay.SelectAsync(null, false); if (result != null) await CaptureManual(result);
    }
    private async void ReadFixed(object sender, RoutedEventArgs e) { try { await CaptureManual(CaptureGeometry.Resolve(settings.SelectedProfile.Area, NativeDesktop.Monitors())); } catch (Exception error) { SetStatus(error.Message); } }
    private async Task CaptureManual(int[] box)
    {
        auto.ManualStarted(); manualCapture?.Cancel(); var job = new CancellationTokenSource(); manualCapture = job; SetStatus("Reading screen text…");
        try { var result = await capture.ReadAsync(box, settings.Clone(), true, job.Token); if (manualCapture == job && !job.IsCancellationRequested) { auto.RememberManual(result.Correction.CorrectedText); AcceptCapture(result, false); } }
        catch (OperationCanceledException) { }
        catch (Exception e) { SetStatus("Capture failed: " + e.Message); }
        finally { if (manualCapture == job) manualCapture = null; job.Dispose(); }
    }
    private void AcceptCapture(CaptureResult result, bool automatic)
    {
        SetText(result.Correction.CorrectedText, false, false); correction = result.Correction; CorrectionsButton.IsEnabled = true;
        if (string.IsNullOrWhiteSpace(Editor.Text)) { SetStatus("No readable text was found."); return; }
        var doc = PrepareDocument(); var options = SpeechOptionsFor(false) with { Mode = automatic ? "replace" : settings.Speech.CaptureMode }; currentReplay = false; currentRequest = speech.Submit(doc, options); mapped.Add(currentRequest); ownerHighWater = currentRequest; if (automatic) autoRequest = currentRequest;
        SetStatus($"OCR {result.Recognition.Variant} · {result.Recognition.Attempts} pass(es) · {result.CaptureMs + result.Recognition.ElapsedMs + result.CorrectionMs:0} ms; preparing speech…");
    }
    public void SetText(string text, bool markdown, bool cancelPlayback = true)
    {
        InvalidateReading(cancelPlayback); programmatic = true; Editor.Text = text; programmatic = false; markdownMode = markdown; revision++; prepared = null; rendered = null; imagesApproved = false; DocumentTabs.SelectedIndex = markdown ? 1 : 0; TextMetadata.Text = $"{text.Length} characters · {Editor.Document.LineCount} lines"; _ = PreparePreview(); UpdateTransport();
    }
    private void EditorChanged(object? sender, EventArgs e)
    {
        if (initializing || programmatic) return; InvalidateReading(); correction = null; revision++; prepared = null; rendered = null; imagesApproved = false; TextMetadata.Text = Editor.Text.Length == 0 ? "Type or paste text, or capture it from the screen." : $"Edited text · {Editor.Text.Length} characters · {Editor.Document.LineCount} lines"; editDebounce.Stop(); editDebounce.Start(); UpdateTransport();
    }
    private void InvalidateReading(bool cancel = true) { if (cancel && currentRequest > 0) speech.Cancel(currentRequest); currentRequest = 0; playbackRevision = rateRevision = 0; paused = false; hasTiming = false; currentOffset = 0; ClearHighlight(); }
    private SpeechDocument PrepareDocument()
    {
        var md = markdownMode || DocumentTabs.SelectedIndex == 1;
        if (prepared == null || preparedRevision != revision || preparedMarkdown != md) { prepared = SpeechText.Prepare(Editor.Text, md); preparedRevision = revision; preparedMarkdown = md; }
        return prepared;
    }
    private Task PreparePreview()
    {
        if (quitting) return Task.CompletedTask;
        var md = markdownMode || DocumentTabs.SelectedIndex == 1;
        // One worker drains the newest document; paste/tab callbacks share its task.
        if (preparationTask is { IsCompleted: false }) { if (preparationRevision != revision || preparationMarkdown != md) preparation?.Cancel(); return preparationTask; }
        if (preparedRevision == revision && preparedMarkdown == md && (!md || rendered != null)) return RenderPreview();
        return preparationTask = PreparePreviewLoop();
    }
    private async Task PreparePreviewLoop()
    {
        while (!quitting)
        {
            var source = Editor.Text; var version = revision; var md = markdownMode || DocumentTabs.SelectedIndex == 1;
            using var job = new CancellationTokenSource(); preparation = job; preparationRevision = version; preparationMarkdown = md;
            var cached = preparedRevision == version && preparedMarkdown == md ? prepared : null;
            try
            {
                var value = await Task.Run(() => { job.Token.ThrowIfCancellationRequested(); var document = cached ?? SpeechText.Prepare(source, md); job.Token.ThrowIfCancellationRequested(); var preview = md ? MarkdownService.Render(source, document) : null; job.Token.ThrowIfCancellationRequested(); return (document, preview); }, job.Token);
                if (quitting) return;
                if (version != revision || source != Editor.Text || md != (markdownMode || DocumentTabs.SelectedIndex == 1)) continue;
                prepared = value.document; preparedRevision = version; preparedMarkdown = md; rendered = value.preview; UpdateTransport(); if (DocumentTabs.SelectedIndex == 1) await RenderPreview();
                if (version != revision || md != (markdownMode || DocumentTabs.SelectedIndex == 1)) continue;
                return;
            }
            catch (OperationCanceledException) { if (quitting) return; }
            catch (Exception error) { SetStatus("Text preparation failed. The editable source was preserved. " + error.Message); return; }
            finally { if (ReferenceEquals(preparation, job)) preparation = null; }
        }
    }
    private async void DocumentTabChanged(object sender, SelectionChangedEventArgs e)
    {
        if (initializing || Preview == null || !ReferenceEquals(e.Source, DocumentTabs)) return;
        Preview.Visibility = DocumentTabs.SelectedIndex == 1 ? Visibility.Visible : Visibility.Collapsed;
        if (DocumentTabs.SelectedIndex == 1) { if (!markdownMode) { markdownMode = true; prepared = null; rendered = null; } await PreparePreview(); }
        else { Editor.TextArea.TextView.Redraw(); RestoreEditHighlight(); _ = Dispatcher.BeginInvoke(DispatcherPriority.Input, () => { if (DocumentTabs.SelectedIndex == 0 && (Keyboard.FocusedElement is TabItem || Keyboard.FocusedElement == DocumentTabs)) Editor.Focus(); }); }
    }
    private async Task EnsurePreview()
    {
        if (previewReady || previewLoading) return; previewLoading = true;
        try
        {
            var folder = Path.Combine(Path.GetDirectoryName(store.PathName)!, "WebView2"); var env = await desktop.CreatePreviewEnvironment(folder); await Preview.EnsureCoreWebView2Async(env);
            var core = Preview.CoreWebView2; core.Settings.AreDevToolsEnabled = false; core.Settings.AreHostObjectsAllowed = false; core.Settings.IsStatusBarEnabled = false;
            core.SetVirtualHostNameToFolderMapping("gtr-preview.local", desktop.PreviewAssetDirectory, CoreWebView2HostResourceAccessKind.DenyCors);
            core.SetVirtualHostNameToFolderMapping("gtr-assets.local", Path.Combine(AppContext.BaseDirectory, "assets", "markdown_preview"), CoreWebView2HostResourceAccessKind.Allow);
            core.AddWebResourceRequestedFilter("*", CoreWebView2WebResourceContext.All);
            core.WebResourceRequested += (_, e) => { if (Uri.TryCreate(e.Request.Uri, UriKind.Absolute, out var uri) && uri.Scheme == "https" && (uri.Host == "gtr-assets.local" || uri.Host == "gtr-preview.local" && new[] { "/index.html", "/preview.js", "/layout.css" }.Contains(uri.AbsolutePath))) return; if (imagesApproved && e.ResourceContext == CoreWebView2WebResourceContext.Image && rendered?.Images.Contains(e.Request.Uri) == true) return; e.Response = env.CreateWebResourceResponse(null, 403, "Blocked", ""); };
            core.NavigationStarting += (_, e) => { if (e.Uri.Split('#')[0] == "https://gtr-preview.local/index.html") return; e.Cancel = true; if (e.IsUserInitiated && !e.Uri.Contains("gtr-assets.local") && !e.Uri.Contains("gtr-preview.local")) OpenExternal(e.Uri); };
            core.NewWindowRequested += (_, e) => { e.Handled = true; if (e.IsUserInitiated) OpenExternal(e.Uri); };
            core.DownloadStarting += (_, e) => e.Cancel = true; core.PermissionRequested += (_, e) => e.State = CoreWebView2PermissionState.Deny;
            core.WebMessageReceived += (_, e) => { if (e.Source.Split('#')[0] != "https://gtr-preview.local/index.html") return; try { using var value = JsonDocument.Parse(e.WebMessageAsJson); var root = value.RootElement; if (root.GetProperty("kind").GetString() == "seek" && root.GetProperty("revision").GetInt64() == revision && root.GetProperty("index").TryGetInt32(out var index)) SeekPreview(index); } catch (Exception error) when (error is JsonException or KeyNotFoundException or InvalidOperationException) { } };
            var loaded = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            EventHandler<CoreWebView2NavigationCompletedEventArgs> navigation = (_, e) =>
            {
                if (!e.IsSuccess || e.HttpStatusCode >= 400) loaded.TrySetException(new IOException($"The local preview page could not load ({e.WebErrorStatus}, HTTP {e.HttpStatusCode})."));
                else loaded.TrySetResult();
            };
            core.NavigationCompleted += navigation;
            try
            {
                core.Navigate("https://gtr-preview.local/index.html"); await loaded.Task.WaitAsync(TimeSpan.FromSeconds(12));
                var available = await core.ExecuteScriptAsync("typeof window.readerRender === 'function' && typeof window.readerHighlight === 'function'");
                if (available != "true") throw new IOException("The local preview assets are missing or incomplete. Extract the complete application ZIP again.");
            }
            finally { core.NavigationCompleted -= navigation; }
            if (quitting) return;
            previewReady = true; PreviewError.Text = "";
        }
        catch (Exception e) { previewReady = false; PreviewError.Text = "Preview is unavailable. Edit and speech still work. Check the Microsoft Edge WebView2 Runtime. " + e.Message; Preview.Visibility = Visibility.Collapsed; LoadImagesButton.Visibility = Visibility.Collapsed; }
        finally { previewLoading = false; }
    }
    private async Task RenderPreview()
    {
        if (quitting || DocumentTabs == null || DocumentTabs.SelectedIndex != 1 || rendered == null) return;
        if (renderingPreview) { previewRenderPending = true; return; } renderingPreview = true;
        try
        {
            do
            {
                previewRenderPending = false; await EnsurePreview(); if (quitting || !previewReady || rendered == null || DocumentTabs.SelectedIndex != 1) return;
                var version = revision; var appearance = themeRevision; var document = rendered;
                if (renderedRevision != version || renderedThemeRevision != appearance)
                {
                    await Preview.ExecuteScriptAsync($"window.readerRender({JsonSerializer.Serialize(document.Html)},{JsonSerializer.Serialize(dark ? "dark" : "light")},{version});");
                    if (quitting) return;
                    if (revision != version || rendered != document || appearance != themeRevision) { previewRenderPending = true; continue; }
                    renderedRevision = version; renderedThemeRevision = appearance;
                }
                LoadImagesButton.Visibility = document.Images.Count > 0 && !imagesApproved ? Visibility.Visible : Visibility.Collapsed;
                if (imagesApproved) await Preview.ExecuteScriptAsync("window.readerLoadImages(" + JsonSerializer.Serialize(document.Images) + ");");
                QueuePreviewHighlight();
            } while (previewRenderPending && !quitting);
        }
        catch (Exception error) when (error is InvalidOperationException or System.Runtime.InteropServices.COMException or ObjectDisposedException) { if (!quitting) PreviewError.Text = "Preview update failed. Edit and speech remain available. " + error.Message; }
        finally { renderingPreview = false; }
    }
    private async void LoadImages(object sender, RoutedEventArgs e) { if (rendered == null || !Dialogs.Confirm(this, "Load remote images", "Load this document’s images from their websites? This shares your network address with those sites.")) return; imagesApproved = true; LoadImagesButton.Visibility = Visibility.Collapsed; await Preview.ExecuteScriptAsync("window.readerLoadImages(" + JsonSerializer.Serialize(rendered.Images) + ");"); }
    private static void OpenExternal(string url) { if (Uri.TryCreate(url, UriKind.Absolute, out var uri) && new[] { "http", "https", "mailto" }.Contains(uri.Scheme)) Process.Start(new ProcessStartInfo(url) { UseShellExecute = true }); }
    private void SeekPreview(int index) { var document = PrepareDocument(); if (renderedRevision != revision || currentRequest == 0 || index < 0 || index >= document.Words.Count) return; playbackRevision = speech.Seek(currentRequest, Editor.Text, document.Words[index].SourceStart, true, true); }
    private void EditorDoubleClick(object sender, MouseButtonEventArgs e) { if (currentRequest == 0) return; var position = Editor.GetPositionFromPoint(e.GetPosition(Editor)); if (position == null) return; var offset = Editor.Document.GetOffset(position.Value.Line, position.Value.Column); playbackRevision = speech.Seek(currentRequest, Editor.Text, offset, true, true); e.Handled = true; }
    private void QueueProgress(SpeechProgress value)
    {
        if (value.RequestId != Volatile.Read(ref currentRequest)) return;
        lock (progressGate)
        {
            if (pendingProgress is { } previous && (value.RequestId < previous.RequestId || value.RequestId == previous.RequestId && value.PlaybackRevision < previous.PlaybackRevision)) return;
            pendingProgress = value; if (progressQueued || Dispatcher.HasShutdownStarted) return; progressQueued = true;
        }
        Dispatcher.BeginInvoke(DispatcherPriority.Render, () => { SpeechProgress? latest; lock (progressGate) { latest = pendingProgress; pendingProgress = null; progressQueued = false; } if (!quitting && latest != null) ShowProgress(latest); });
    }
    private void ShowProgress(SpeechProgress value)
    {
        if (value.RequestId != currentRequest || value.SourceText != Editor.Text || value.SourceStart < 0 || value.SourceEnd <= value.SourceStart || value.SourceEnd > Editor.Text.Length || value.PlaybackRevision < playbackRevision) return;
        playbackRevision = value.PlaybackRevision;
        var oldStart = marker.Start; var oldEnd = marker.End; currentOffset = value.SourceStart; highlightedWord = value.WordIndex; marker.Start = value.SourceStart; marker.End = value.SourceEnd;
        if (DocumentTabs.SelectedIndex == 0) { RedrawMarker(oldStart, oldEnd); RestoreEditHighlight(); }
        QueuePreviewHighlight();
        var sentence = prepared == null ? 0 : SentenceIndex(prepared); if (sentence != transportSentence) { transportSentence = sentence; UpdateTransport(); }
    }
    private void RedrawMarker(int start, int end) { if (start >= 0 && end > start && end <= Editor.Document.TextLength) Editor.TextArea.TextView.Redraw(start, end - start, DispatcherPriority.Render); Editor.TextArea.TextView.InvalidateLayer(ICSharpCode.AvalonEdit.Rendering.KnownLayer.Selection); }
    private void RestoreEditHighlight() { if (marker.Start < 0 || marker.End > Editor.Document.TextLength) return; RedrawMarker(marker.Start, marker.End); var location = Editor.Document.GetLocation(marker.Start); Editor.ScrollTo(location.Line, location.Column); }
    private void ClearHighlight() { var start = marker.Start; var end = marker.End; marker.Start = -1; highlightedWord = -1; transportSentence = -1; previewIndexDocument = null; previewWordIndices.Clear(); if (Editor != null) RedrawMarker(start, end); QueuePreviewHighlight(); }
    private void QueuePreviewHighlight()
    {
        if (marker.Start >= 0) highlightedWord = PreviewWordIndex(marker.Start, marker.End, highlightedWord);
        highlightSequence++; highlightScriptPending = true;
        if (!highlightScriptRunning && previewReady && DocumentTabs.SelectedIndex == 1 && renderedRevision == revision) _ = FlushPreviewHighlight();
    }
    private int PreviewWordIndex(int start, int end, int candidate)
    {
        if (prepared == null || preparedRevision != revision) return -1;
        if (candidate >= 0 && candidate < prepared.Words.Count && prepared.Words[candidate].SourceStart == start && prepared.Words[candidate].SourceEnd == end) return candidate;
        // A plain capture may still be speaking when Preview is opened. Only an
        // exact source span can translate that reading into the rendered index.
        if (!ReferenceEquals(previewIndexDocument, prepared))
        {
            previewIndexDocument = prepared; previewWordIndices.Clear();
            for (var i = 0; i < prepared.Words.Count; i++) { var word = prepared.Words[i]; previewWordIndices.TryAdd((word.SourceStart, word.SourceEnd), i); }
        }
        return previewWordIndices.TryGetValue((start, end), out var index) ? index : -1;
    }
    private async Task FlushPreviewHighlight()
    {
        highlightScriptRunning = true;
        try
        {
            while (highlightScriptPending && !quitting && previewReady && DocumentTabs.SelectedIndex == 1 && renderedRevision == revision)
            {
                highlightScriptPending = false; var documentVersion = revision; var request = currentRequest; var playback = playbackRevision; var sequence = highlightSequence; var index = highlightedWord;
                await Preview.ExecuteScriptAsync($"window.readerHighlight({index},{documentVersion},{request},{playback},{sequence});");
            }
        }
        catch (Exception error) when (error is InvalidOperationException or System.Runtime.InteropServices.COMException or ObjectDisposedException) { if (!quitting) DebugLog("Preview highlight update unavailable: " + error.GetType().Name); }
        finally { highlightScriptRunning = false; }
    }
    internal void AcceptRateResult(SpeechRateResult result)
    {
        if (result.RequestId != currentRequest || result.Revision < rateRevision || Math.Abs(result.Requested - settings.Speech.ReaderPlaybackRate) > .02) return;
        if (result.Error != null) { initializing = true; ReaderSpeed.Value = result.Actual; ReaderSpeedText.Text = result.Actual.ToString("0.0") + "×"; initializing = false; settings.Speech.ReaderPlaybackRate = result.Actual; Persist(); SetStatus(result.Error); }
    }
    private SpeechOptions SpeechOptionsFor(bool reader, bool preview = false) => new(settings.Voice, settings.Rate, preview ? Math.Min(60, settings.Volume) : settings.Volume, settings.Speech.CaptureMode, settings.Speech.MaxOverlap, reader ? settings.Speech.ReaderPlaybackRate : 1, reader, preview);
    private void PlayPause(object sender, RoutedEventArgs e) { if (currentRequest > 0) playbackRevision = speech.Pause(currentRequest, Editor.Text, !paused); else StartReader(false); }
    private void ReadAgain(object sender, RoutedEventArgs e) => StartReader(true);
    private void StartReader(bool restart)
    {
        if (string.IsNullOrWhiteSpace(Editor.Text)) { SetStatus("Type, paste, or capture text first."); return; }
        if (restart) speech.StopAll(); currentReplay = true; paused = false; currentOffset = 0; playbackRevision = rateRevision = 0; currentRequest = speech.Submit(PrepareDocument(), SpeechOptionsFor(true)); mapped.Add(currentRequest); ownerHighWater = currentRequest; SetStatus("Preparing speech…"); UpdateTransport();
    }
    private void PreviousSentence(object sender, RoutedEventArgs e) => Navigate(-1);
    private void NextSentence(object sender, RoutedEventArgs e) => Navigate(1);
    private void Navigate(int direction) { if (currentRequest == 0 || !hasTiming) { SetStatus("This voice supplies no usable word timing for sentence navigation."); return; } var doc = PrepareDocument(); var index = SentenceIndex(doc); var target = index + direction; if (target < 0 || target >= doc.Sentences.Count) return; currentOffset = doc.Sentences[target].SourceStart; playbackRevision = speech.Seek(currentRequest, Editor.Text, currentOffset, false, true); UpdateTransport(); }
    private int SentenceIndex(SpeechDocument document) { int low = 0, high = document.Sentences.Count - 1, found = 0; while (low <= high) { int middle = low + (high - low) / 2; if (document.Sentences[middle].SourceStart <= currentOffset) { found = middle; low = middle + 1; } else high = middle - 1; } return found; }
    private void UpdateTransport() { if (PlayButton == null) return; var text = currentRequest == 0 || paused ? "Play" : "Pause"; if (ButtonText(PlayButton) != text) PlayButton.Content = text; DecorateButton(PlayButton, currentRequest != 0 && !paused ? "pause" : "play"); PlayButton.IsEnabled = !string.IsNullOrWhiteSpace(Editor.Text); var doc = prepared; var index = doc == null ? 0 : SentenceIndex(doc); transportSentence = index; PreviousButton.IsEnabled = currentRequest > 0 && hasTiming && index > 0; NextButton.IsEnabled = currentRequest > 0 && hasTiming && doc != null && index < doc.Sentences.Count - 1; PreviousButton.ToolTip = NextButton.ToolTip = hasTiming ? "Move to the adjacent spoken sentence" : "Sentence navigation needs word timing from the voice"; CorrectionsButton.IsEnabled = correction != null; }
    private void ReaderSpeedChanged(object sender, RoutedPropertyChangedEventArgs<double> e) { if (initializing || ReaderSpeedText == null) return; settings.Speech.ReaderPlaybackRate = Math.Round(e.NewValue, 1); ReaderSpeedText.Text = settings.Speech.ReaderPlaybackRate.ToString("0.0") + "×"; speedDebounce.Stop(); speedDebounce.Start(); }
    private void ResetReaderSpeed(object sender, RoutedEventArgs e) => ReaderSpeed.Value = 1;
    public void ClearReader() { manualCapture?.Cancel(); InvalidateReading(); correction = null; SetText("", false); TextMetadata.Text = "Type or paste text, or capture it from the screen."; DocumentTabs.SelectedIndex = 0; Preview.Visibility = Visibility.Collapsed; Dispatcher.BeginInvoke(DispatcherPriority.Input, () => { if (DocumentTabs.SelectedIndex == 0) { Editor.UpdateLayout(); Editor.Focus(); Keyboard.Focus(Editor.TextArea); } }); SetStatus("Text cleared."); }
    private void ClearText(object sender, RoutedEventArgs e) => ClearReader();
    private void CopyText(object sender, RoutedEventArgs e) { if (Editor.Text.Length > 0) Clipboard.SetText(Editor.Text); }
    private void StopAudio(object sender, RoutedEventArgs e) { auto.Stop(); speech.StopAll(); InvalidateReading(); SetStatus("Audio and Auto-Read stopped."); }
    private void OcrModeChanged(object sender, SelectionChangedEventArgs e) { if (initializing || OcrMode.SelectedItem is not string s) return; settings.Ocr.RecognitionMode = s.ToLowerInvariant(); Persist(); }
    private void AutoSpeedChanged(object sender, SelectionChangedEventArgs e) { if (initializing || AutoSpeed.SelectedItem is not string s) return; settings.AutoRead.Speed = s.ToLowerInvariant(); if (!Persist()) { initializing = true; AutoSpeed.SelectedItem = TitleCase(settings.AutoRead.Speed); initializing = false; } }
    private void ToggleAuto(object sender, RoutedEventArgs e) { if (auto.Active) { auto.Stop(); AutoButton.Content = "Start Auto-Read"; AutoStatus.Text = "Auto-Read is off."; } else { try { var area = settings.SelectedProfile.Area ?? throw new InvalidOperationException("Set a capture area first."); _ = CaptureGeometry.Resolve(area, NativeDesktop.Monitors()); auto.Start(area); AutoButton.Content = "Stop Auto-Read"; } catch (Exception error) { SetStatus(error.Message); } } }
    private void CorrectionOptionsChanged(object sender, RoutedEventArgs e) { if (initializing) return; settings.Ocr.Enabled = CorrectionEnabled.IsChecked == true; settings.Ocr.DebugLogging = DebugLogging.IsChecked == true; Persist(); }
    private void CorrectionStrengthChanged(object sender, SelectionChangedEventArgs e) { if (initializing || CorrectionStrength.SelectedItem is not string s) return; settings.Ocr.Strength = s.ToLowerInvariant(); Persist(); }
    private void SaveProtected(object sender, RoutedEventArgs e) { settings.Ocr.ProtectedWords = ProtectedTerms.Text.Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries).ToList(); Persist(); SetStatus("Protected terms saved."); }
    private void AddRule(object sender, RoutedEventArgs e) => EditReplacement(null);
    private void EditRule(object sender, RoutedEventArgs e) { if (Rules.SelectedItem is ReplacementRule rule) EditReplacement(rule); }
    private void RuleDoubleClick(object sender, MouseButtonEventArgs e) => EditRule(sender, e);
    private void DeleteRule(object sender, RoutedEventArgs e) { if (Rules.SelectedItem is ReplacementRule rule) { settings.Ocr.Replacements.Remove(rule); Persist(); Rules.ItemsSource = settings.Ocr.Replacements.ToArray(); } }
    private ReplacementRule? EditReplacement(ReplacementRule? existing, string original = "")
    {
        var rule = Dialogs.Replacement(this, existing, original, text => speech.Submit(SpeechText.Prepare(text), SpeechOptionsFor(false, true)));
        if (rule == null) return null; if (existing != null) settings.Ocr.Replacements[settings.Ocr.Replacements.IndexOf(existing)] = rule; else settings.Ocr.Replacements.Add(rule); if (!Persist()) return null; Rules.ItemsSource = settings.Ocr.Replacements.ToArray(); return rule;
    }
    private void ShowCorrections(object sender, RoutedEventArgs e) { if (correction is not { } opened) return; var version = revision; Dialogs.Corrections(this, opened, result => { if (correction == null || revision != version) return; if (Editor.Text != result.CorrectedText) { SetText(result.CorrectedText, false); version = revision; } correction = result; UpdateTransport(); }); }
    private void VoiceChanged(object sender, SelectionChangedEventArgs e) { if (initializing || Voices.SelectedItem is not VoiceInfo voice) return; settings.Voice = voice.Id; Persist(); }
    private void BaseVoiceChanged(object sender, RoutedPropertyChangedEventArgs<double> e) { if (initializing || VolumeLabel == null) return; settings.Rate = (int)BaseSpeed.Value; settings.Volume = Volume.Value; BaseSpeedLabel.Text = "Base speed: " + settings.Rate; VolumeLabel.Text = "Volume: " + (int)settings.Volume; Persist(); }
    private void SpeechModeChanged(object sender, SelectionChangedEventArgs e) { if (initializing) return; settings.Speech.CaptureMode = SpeechMode.SelectedIndex switch { 1 => "queue", 2 => "overlap", _ => "replace" }; settings.Speech.MaxOverlap = MaxOverlap.SelectedItem is int n ? n : 2; if (Persist()) speech.StopAll(); }
    private void TestVoice(object sender, RoutedEventArgs e) => speech.Submit(SpeechText.Prepare("This is a preview of the selected voice."), SpeechOptionsFor(false, true));
    private void BuildShortcutRows()
    {
        ShortcutGrid.ColumnDefinitions.Add(new() { Width = GridLength.Auto }); ShortcutGrid.ColumnDefinitions.Add(new() { Width = new GridLength(1, GridUnitType.Star) }); ShortcutGrid.ColumnDefinitions.Add(new() { Width = GridLength.Auto }); ShortcutGrid.ColumnDefinitions.Add(new() { Width = GridLength.Auto });
        string[] names = ["Read fixed box", "Select a snippet", "Read Again", "Toggle Auto-Read"];
        for (int i = 0; i < 4; i++)
        {
            ShortcutGrid.RowDefinitions.Add(new() { Height = GridLength.Auto }); int index = i; var box = new TextBox { IsReadOnly = true, ToolTip = names[i], Margin = new Thickness(3, 5, 6, 5), Height = 36, VerticalContentAlignment = VerticalAlignment.Center, MinWidth = 70 }; shortcutBoxes[i] = box; var record = new Button { Content = "Record", Tag = "record", Height = 36, Padding = new Thickness(10, 5, 10, 5), VerticalAlignment = VerticalAlignment.Center }; var clear = new Button { Content = "Clear", Tag = "delete", Height = 36, Padding = new Thickness(10, 5, 10, 5), VerticalAlignment = VerticalAlignment.Center };
            System.Windows.Automation.AutomationProperties.SetName(box, names[i] + " shortcut");
            record.Click += (_, _) => { var result = Dialogs.RecordShortcut(this, names[index]); if (result != null) { box.Text = result; PendingShortcuts.Text = "Recorded changes — click Apply shortcuts."; } }; clear.Click += (_, _) => { box.Text = ""; PendingShortcuts.Text = "Changes are not applied yet."; };
            var label = new TextBlock { Text = names[i], Margin = new Thickness(3, 5, 12, 5), VerticalAlignment = VerticalAlignment.Center }; Grid.SetRow(label, i); ShortcutGrid.Children.Add(label); Grid.SetRow(box, i); Grid.SetColumn(box, 1); ShortcutGrid.Children.Add(box); Grid.SetRow(record, i); Grid.SetColumn(record, 2); ShortcutGrid.Children.Add(record); Grid.SetRow(clear, i); Grid.SetColumn(clear, 3); ShortcutGrid.Children.Add(clear);
        }
    }
    private void ApplyShortcuts(object sender, RoutedEventArgs e)
    {
        var old = settings.Hotkeys; try { var values = shortcutBoxes.Select(b => HotkeyParser.Parse(b.Text)?.Display ?? "").ToArray(); hotkeys?.Apply(values); settings.Hotkeys = new() { Fixed = values[0], Snippet = values[1], ReadAgain = values[2], AutoRead = values[3] }; if (!Persist()) { hotkeys?.Apply(old.Values()); return; } PendingShortcuts.Text = "Shortcuts are active."; ShowShortcutStatus(); } catch (Exception error) { SetStatus(error.Message); }
    }
    private void ApplyActiveHotkeys() { try { hotkeys?.Apply(settings.Hotkeys.Values()); ShowShortcutStatus(); } catch (Exception error) { SetStatus(error.Message); ShortcutStatus.Text = "Shortcuts need attention"; } }
    private void ShowShortcutStatus() => ShortcutStatus.Text = "Shortcuts ready · " + string.Join(" / ", settings.Hotkeys.Values().Where(k => k.Length > 0));
    private nint WindowMessage(nint hwnd, int message, nint wparam, nint lparam, ref bool handled)
    {
        if (message == 0x001A && settings.Theme == "system") Ui(ApplyTheme); return 0;
    }
    private void DispatchShortcut(int index)
    {
        if (quitting) return;
        DebugLog("Shortcut dispatched: " + index);
        switch (index) { case 0: if (IsVisible) WindowState = WindowState.Minimized; ReadFixed(this, new()); break; case 1: if (IsVisible) WindowState = WindowState.Minimized; SelectSnippet(this, new()); break; case 2: ReadAgain(this, new()); break; case 3: ToggleAuto(this, new()); break; }
    }
    private void StartupChanged(object sender, RoutedEventArgs e)
    {
        if (initializing) return; var enabled = StartupEnabled.IsChecked == true; var old = settings.Startup.Enabled;
        try { if (!noIntegrations) new StartupRegistration().SetEnabled(enabled, Environment.ProcessPath!); settings.Startup.Enabled = enabled; if (!Persist()) throw new IOException("The enabled preference could not be saved."); }
        catch (Exception error) { settings.Startup.Enabled = old; StartupEnabled.IsChecked = old; if (!noIntegrations) try { new StartupRegistration().SetEnabled(old, Environment.ProcessPath!); } catch { } Dialogs.Info(this, "Startup could not be changed", error.Message + "\nCheck your Windows account policy or Startup Apps settings."); }
    }
    private void ImportSettings(object sender, RoutedEventArgs e) { var dialog = new Microsoft.Win32.OpenFolderDialog { Title = "Choose the older Game Text Reader app folder" }; if (dialog.ShowDialog(this) != true) return; try { store.Import(dialog.FolderName); Dialogs.Info(this, "Settings imported", "A backup was kept. Restart the app to load all imported settings."); } catch (Exception error) { Dialogs.Info(this, "Import failed", error.Message); } }
    private void CreateTray()
    {
        try
        {
            tray = desktop.CreateTrayIcon(); var menu = new System.Windows.Forms.ContextMenuStrip();
            void Add(string label, Action action) => menu.Items.Add(label, null, (_, _) => Ui(action));
            Add("Show settings", ShowSettings); Add("Read fixed box now", () => ReadFixed(this, new())); Add("Select a snippet", () => SelectSnippet(this, new())); Add("Read Again", () => ReadAgain(this, new())); Add("Toggle Auto-Read", () => ToggleAuto(this, new())); Add("Stop audio", () => StopAudio(this, new())); Add("Hide settings to tray", Hide); menu.Items.Add(new System.Windows.Forms.ToolStripSeparator()); Add("Quit", Quit); tray.ContextMenuStrip = menu; tray.DoubleClick += (_, _) => Ui(ShowSettings);
        }
        catch (Exception error) { tray?.Dispose(); tray = null; SetStatus("Tray unavailable: " + error.Message); Show(); }
    }
    private void ShowSettings() { Show(); WindowState = settings.Window.State == "maximized" ? WindowState.Maximized : WindowState.Normal; Activate(); }
    private void HideToTray(object sender, RoutedEventArgs e) { if (tray != null) Hide(); else SetStatus("The tray icon is unavailable; settings remain visible."); }
    private void WindowClosing(object? sender, CancelEventArgs e)
    {
        if (!quitting) { SaveWindow(); e.Cancel = true; WindowState = WindowState.Minimized; return; }
        SaveWindow(); editDebounce.Stop(); speedDebounce.Stop(); preparation?.Cancel(); manualCapture?.Cancel(); auto.Dispose(); hotkeys?.Dispose(); tray?.Dispose(); speech.Dispose(); Preview.Dispose();
    }
    private void RestorePhysicalPlacement()
    {
        restoringWindow = true; var monitors = NativeDesktop.Monitors(); var monitor = monitors.FirstOrDefault(m => m.Contains(settings.Window.X ?? int.MinValue, settings.Window.Y ?? int.MinValue)) ?? monitors.FirstOrDefault(m => m.Primary) ?? monitors[0]; var working = System.Windows.Forms.Screen.AllScreens.FirstOrDefault(s => s.DeviceName == monitor.Device)?.WorkingArea ?? new System.Drawing.Rectangle(monitor.Left, monitor.Top, monitor.Width, monitor.Height);
        var width = Math.Min(working.Width, (int)(Width * monitor.DpiX / 96)); var height = Math.Min(working.Height, (int)(Height * monitor.DpiY / 96)); var x = Math.Clamp(settings.Window.X ?? working.Left + (working.Width - width) / 2, working.Left, Math.Max(working.Left, working.Right - width)); var y = Math.Clamp(settings.Window.Y ?? working.Top + (working.Height - height) / 2, working.Top, Math.Max(working.Top, working.Bottom - height)); normalBounds = [x, y, x + width, y + height]; NativeDesktop.SetWindowPos(new WindowInteropHelper(this).Handle, 0, x, y, width, height, 0x0014); if (settings.Window.State == "maximized") WindowState = WindowState.Maximized; restoringWindow = false;
    }
    private void RememberPhysicalPlacement() { if (initializing || restoringWindow || WindowState != WindowState.Normal || !IsVisible) return; var hwnd = new WindowInteropHelper(this).Handle; if (hwnd != 0 && NativeDesktop.GetWindowRect(hwnd, out var r)) normalBounds = [r.Left, r.Top, r.Right, r.Bottom]; }
    private void SaveWindow() { if (initializing || WindowState == WindowState.Minimized) return; RememberPhysicalPlacement(); if (normalBounds is not { } b) return; settings.Window = new() { Width = b[2] - b[0], Height = b[3] - b[1], X = b[0], Y = b[1], State = WindowState == WindowState.Maximized ? "maximized" : "normal" }; Persist(); }
    public void Quit() { quitting = true; Close(); Application.Current.Shutdown(); }
    private void BuildContextMenu()
    {
        var menu = new ContextMenu();
        void Add(string name, Action action) { var item = new MenuItem { Header = name }; item.Click += (_, _) => action(); menu.Items.Add(item); }
        Add("Copy", () => Editor.Copy()); Add("Cut", () => Editor.Cut()); Add("Paste", () => Editor.Paste()); Add("Select All", Editor.SelectAll); menu.Items.Add(new Separator()); Add("Replace Word…", () => RuleFromSelection(true)); Add("Add to Replacement Rules…", () => RuleFromSelection(false));
        menu.Opened += (_, _) => { menu.Background = (Brush)FindResource("SurfaceBrush"); menu.Foreground = (Brush)FindResource("TextBrush"); foreach (var item in menu.Items.OfType<MenuItem>()) { item.Background = menu.Background; item.Foreground = menu.Foreground; } var has = Editor.SelectionLength > 0; menu.Items.OfType<MenuItem>().Where(i => i.Header.ToString()!.Contains("Replacement") || i.Header.ToString()!.StartsWith("Replace Word")).ToList().ForEach(i => i.IsEnabled = has); };
        Editor.PreviewMouseRightButtonDown += (_, e) => { if (Editor.SelectionLength > 0) return; var p = Editor.GetPositionFromPoint(e.GetPosition(Editor)); if (p == null) return; var offset = Editor.Document.GetOffset(p.Value.Line, p.Value.Column); var word = SpeechText.WordPattern.Matches(Editor.Text).FirstOrDefault(m => m.Index <= offset && m.Index + m.Length >= offset); if (word != null) Editor.Select(word.Index, word.Length); };
        Editor.ContextMenu = menu;
    }
    private void RuleFromSelection(bool replace)
    {
        if (Editor.SelectionLength == 0) return; var original = Editor.SelectedText; var start = Editor.SelectionStart; var length = Editor.SelectionLength; var source = Editor.Text;
        var existing = settings.Ocr.Replacements.FirstOrDefault(r => r.WholeWord && r.Original.Equals(original, r.CaseSensitive ? StringComparison.Ordinal : StringComparison.OrdinalIgnoreCase)); var rule = EditReplacement(existing, original);
        if (rule != null && replace && Editor.Text == source) { InvalidateReading(); Editor.Document.Replace(start, length, rule.Replacement); }
    }
    public void DecorateButtons(DependencyObject parent)
    {
        for (int i = 0; i < VisualTreeHelper.GetChildrenCount(parent); i++) { var child = VisualTreeHelper.GetChild(parent, i); if (child is Button button && button.Tag is string icon) DecorateButton(button, icon); DecorateButtons(child); }
    }
    public void DecorateButton(Button button, string icon)
    {
        var text = ButtonText(button); var palette = button.Background == FindResource("AccentBrush") ? "primary" : dark ? "dark" : "light"; var decoration = new ButtonDecoration(text, icon, palette);
        if (decorations.TryGetValue(button, out var previous) && previous == decoration && button.Content is StackPanel) return;
        var key = palette + "/" + icon;
        if (!icons.TryGetValue(key, out var source))
        {
            if (icon == "repeat")
            {
                var geometry = Geometry.Parse("M 18 7 A 8 8 0 1 0 19 15 M 18 1 L 18 7 L 12 7"); var pen = new Pen(palette == "primary" ? Brushes.White : (Brush)FindResource("TextBrush"), 1.8) { StartLineCap = PenLineCap.Round, EndLineCap = PenLineCap.Round, LineJoin = PenLineJoin.Round }; var drawing = new GeometryDrawing(null, pen, geometry); drawing.Freeze(); source = new DrawingImage(drawing);
            }
            else
            {
                var path = Path.Combine(AppContext.BaseDirectory, "assets", "action_icons", palette, "24", icon + ".png"); if (!File.Exists(path)) return;
                var bitmap = new BitmapImage(); bitmap.BeginInit(); bitmap.UriSource = new Uri(path); bitmap.CacheOption = BitmapCacheOption.OnLoad; bitmap.EndInit(); source = bitmap;
            }
            source.Freeze(); icons.Add(key, source);
        }
        var content = new StackPanel { Orientation = Orientation.Horizontal }; content.Children.Add(new Image { Source = source, Width = 18, Height = 18, Margin = new Thickness(0, 0, 7, 0) }); content.Children.Add(new TextBlock { Text = text, Foreground = palette == "primary" ? Brushes.White : (Brush)FindResource("TextBrush"), VerticalAlignment = VerticalAlignment.Center }); button.Content = content; button.Tag = icon; System.Windows.Automation.AutomationProperties.SetName(button, text); decorations.Remove(button); decorations.Add(button, decoration);
    }
    private static string ButtonText(Button button) => button.Content is StackPanel panel ? panel.Children.OfType<TextBlock>().LastOrDefault()?.Text ?? "" : button.Content?.ToString() ?? "";
}
