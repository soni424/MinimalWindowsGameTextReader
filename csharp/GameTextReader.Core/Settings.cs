using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

namespace GameTextReader.Core;

public sealed class AppSettings
{
    [JsonPropertyName("theme")] public string Theme { get; set; } = "system";
    [JsonPropertyName("voice")] public string Voice { get; set; } = "";
    [JsonPropertyName("rate")] public int Rate { get; set; }
    [JsonPropertyName("volume")] public double Volume { get; set; } = 100;
    [JsonPropertyName("speech")] public SpeechSettings Speech { get; set; } = new();
    [JsonPropertyName("auto_read")] public AutoReadSettings AutoRead { get; set; } = new();
    [JsonPropertyName("startup")] public StartupSettings Startup { get; set; } = new();
    [JsonPropertyName("fixed_box")] public int[]? FixedBox { get; set; }
    [JsonPropertyName("window")] public WindowSettings Window { get; set; } = new();
    [JsonPropertyName("capture_profiles")] public List<CaptureProfile> Profiles { get; set; } = [];
    [JsonPropertyName("selected_profile_id")] public string SelectedProfileId { get; set; } = "default";
    [JsonPropertyName("hotkeys")] public HotkeySettings Hotkeys { get; set; } = new();
    [JsonPropertyName("ocr")] public OcrSettings Ocr { get; set; } = new();
    [JsonExtensionData] public Dictionary<string, JsonElement>? Extra { get; set; }
    [JsonIgnore] public CaptureProfile SelectedProfile => Profiles.First(p => p.Id == SelectedProfileId);
    public AppSettings Clone() => SettingsCodec.Read(SettingsCodec.Write(this));
}
public sealed class SpeechSettings
{
    [JsonPropertyName("capture_mode")] public string CaptureMode { get; set; } = "replace";
    [JsonPropertyName("max_overlap")] public int MaxOverlap { get; set; } = 2;
    [JsonPropertyName("reader_playback_rate")] public double ReaderPlaybackRate { get; set; } = 1;
}
public sealed class AutoReadSettings { [JsonPropertyName("speed")] public string Speed { get; set; } = "normal"; }
public sealed class StartupSettings { [JsonPropertyName("enabled")] public bool Enabled { get; set; } }
public sealed class WindowSettings
{
    [JsonPropertyName("width")] public int Width { get; set; } = 1200;
    [JsonPropertyName("height")] public int Height { get; set; } = 860;
    [JsonPropertyName("x")] public int? X { get; set; }
    [JsonPropertyName("y")] public int? Y { get; set; }
    [JsonPropertyName("state")] public string State { get; set; } = "normal";
}
public sealed class HotkeySettings
{
    [JsonPropertyName("fixed")] public string Fixed { get; set; } = "Alt+Z";
    [JsonPropertyName("snippet")] public string Snippet { get; set; } = "Alt+S";
    [JsonPropertyName("read_again")] public string ReadAgain { get; set; } = "";
    [JsonPropertyName("auto_read")] public string AutoRead { get; set; } = "";
    public string[] Values() => [Fixed, Snippet, ReadAgain, AutoRead];
}
public sealed class OcrSettings
{
    [JsonPropertyName("recognition_mode")] public string RecognitionMode { get; set; } = "standard";
    [JsonPropertyName("enabled")] public bool Enabled { get; set; } = true;
    [JsonPropertyName("strength")] public string Strength { get; set; } = "conservative";
    [JsonPropertyName("debug_logging")] public bool DebugLogging { get; set; }
    [JsonPropertyName("replacements")] public List<ReplacementRule> Replacements { get; set; } = [];
    [JsonPropertyName("protected_words")] public List<string> ProtectedWords { get; set; } = [];
}
public sealed class ReplacementRule
{
    [JsonPropertyName("original")] public string Original { get; set; } = "";
    [JsonPropertyName("replacement")] public string Replacement { get; set; } = "";
    [JsonPropertyName("enabled")] public bool Enabled { get; set; } = true;
    [JsonPropertyName("case_sensitive")] public bool CaseSensitive { get; set; }
    [JsonPropertyName("whole_word")] public bool WholeWord { get; set; } = true;
}
public sealed class CaptureProfile
{
    [JsonPropertyName("id")] public string Id { get; set; } = "default";
    [JsonPropertyName("name")] public string Name { get; set; } = "Default";
    [JsonPropertyName("capture_area")] public CaptureArea? Area { get; set; }
    [JsonPropertyName("settings")] public JsonObject Settings { get; set; } = new();
    public override string ToString() => Name;
}
public sealed class CaptureArea
{
    [JsonPropertyName("box")] public int[] Box { get; set; } = [];
    [JsonPropertyName("monitor_device")] public string MonitorDevice { get; set; } = "";
    [JsonPropertyName("monitor_bounds")] public int[]? MonitorBounds { get; set; }
    [JsonPropertyName("monitor_dpi")] public int[]? MonitorDpi { get; set; }
    [JsonPropertyName("relative_box")] public double[]? RelativeBox { get; set; }
}

public static class SettingsCodec
{
    public static readonly JsonSerializerOptions Json = new() { WriteIndented = true, PropertyNameCaseInsensitive = false };
    private static string Choice(string? value, string fallback, params string[] choices) => choices.Contains(value?.Trim().ToLowerInvariant()) ? value!.Trim().ToLowerInvariant() : fallback;
    public static AppSettings Read(string json)
    {
        var root = JsonNode.Parse(json) as JsonObject ?? throw new InvalidDataException("Settings must be a JSON object.");
        var defaults = JsonSerializer.SerializeToNode(new AppSettings(), Json)!.AsObject();
        void Sanitize(JsonObject actual, JsonObject expected)
        {
            foreach (var entry in actual.ToList())
            {
                if (!expected.TryGetPropertyValue(entry.Key, out var sample) || sample == null) continue;
                if (sample is JsonObject model) { if (entry.Value is JsonObject child) Sanitize(child, model); else actual.Remove(entry.Key); }
                else if (sample is JsonArray) { if (entry.Value is not JsonArray) actual.Remove(entry.Key); }
                else if (sample is JsonValue && entry.Value is JsonValue value && sample.GetValueKind() == value.GetValueKind())
                {
                    if (sample.GetValueKind() == JsonValueKind.Number && entry.Key is not ("volume" or "reader_playback_rate") && sample.ToJsonString().IndexOf('.') < 0 && value.TryGetValue<double>(out var number) && double.IsFinite(number)) actual[entry.Key] = (long)Math.Clamp(number, int.MinValue, int.MaxValue);
                }
                else actual.Remove(entry.Key);
            }
        }
        Sanitize(root, defaults);
        if (root["window"] is JsonObject window)
        {
            foreach (var key in new[] { "x", "y" })
                if (window[key] is JsonValue value)
                {
                    if (value.TryGetValue<double>(out var number) && double.IsFinite(number)) window[key] = (int)Math.Clamp(number, -100000, 100000);
                    else window.Remove(key);
                }
                else if (window[key] != null) window.Remove(key);
        }
        if (root["capture_profiles"] is JsonArray profileNodes)
        {
            for (int i = profileNodes.Count - 1; i >= 0; i--)
            {
                if (profileNodes[i] is not JsonObject profile || profile["id"]?.GetValueKind() != JsonValueKind.String || string.IsNullOrWhiteSpace(profile["id"]!.GetValue<string>())) { profileNodes.RemoveAt(i); continue; }
                if (profile["capture_area"] == null && profile["box"] is JsonArray box) profile["capture_area"] = new JsonObject { ["box"] = box.DeepClone() };
                Sanitize(profile, JsonSerializer.SerializeToNode(new CaptureProfile(), Json)!.AsObject());
                if (profile["capture_area"] is JsonObject area)
                {
                    Sanitize(area, JsonSerializer.SerializeToNode(new CaptureArea(), Json)!.AsObject());
                    foreach (var key in new[] { "monitor_bounds", "monitor_dpi", "relative_box" })
                    {
                        try { if (key == "relative_box") _ = area[key]?.Deserialize<double[]>(Json); else _ = area[key]?.Deserialize<int[]>(Json); }
                        catch (JsonException) { area.Remove(key); }
                    }
                    try { _ = area.Deserialize<CaptureArea>(Json); } catch (JsonException) { profile["capture_area"] = null; }
                }
            }
        }
        if (root["ocr"] is JsonObject ocr)
        {
            if (ocr["replacements"] is JsonArray rules) for (int i = rules.Count - 1; i >= 0; i--) { if (rules[i] is not JsonObject rule || rule["original"]?.GetValueKind() != JsonValueKind.String || rule["replacement"]?.GetValueKind() != JsonValueKind.String) { rules.RemoveAt(i); continue; } Sanitize(rule, JsonSerializer.SerializeToNode(new ReplacementRule(), Json)!.AsObject()); }
            if (ocr["protected_words"] is JsonArray terms) for (int i = terms.Count - 1; i >= 0; i--) if (terms[i]?.GetValueKind() != JsonValueKind.String) terms.RemoveAt(i);
        }
        // Invalid individual values must not prevent recovery of the rest of a saved profile.
        foreach (var entry in root.ToList())
        {
            if (defaults[entry.Key] is JsonValue expected && entry.Value is JsonValue value && expected.GetValueKind() != value.GetValueKind()) root.Remove(entry.Key);
        }
        AppSettings settings;
        try { settings = root.Deserialize<AppSettings>(Json) ?? new(); }
        catch (JsonException)
        {
            // Validate fields independently, preserving valid sections around a malformed one.
            var safe = new JsonObject();
            foreach (var entry in root)
            {
                var trial = (JsonObject)safe.DeepClone(); trial[entry.Key] = entry.Value?.DeepClone();
                try { _ = trial.Deserialize<AppSettings>(Json); safe = trial; } catch (JsonException) { }
            }
            settings = safe.Deserialize<AppSettings>(Json) ?? new();
        }
        return Normalize(settings);
    }
    public static string Write(AppSettings settings) => JsonSerializer.Serialize(Normalize(settings), Json);
    public static AppSettings Normalize(AppSettings s)
    {
        s.Theme = Choice(s.Theme, "system", "system", "dark", "light"); s.Voice = (s.Voice ?? "")[..Math.Min((s.Voice ?? "").Length, 1024)];
        s.Rate = Math.Clamp(s.Rate, -10, 10); s.Volume = double.IsFinite(s.Volume) ? Math.Clamp(s.Volume, 0, 100) : 100;
        s.Speech ??= new(); s.Speech.CaptureMode = Choice(s.Speech.CaptureMode, "replace", "replace", "queue", "overlap");
        s.Speech.MaxOverlap = Math.Clamp(s.Speech.MaxOverlap, 2, 4); s.Speech.ReaderPlaybackRate = double.IsFinite(s.Speech.ReaderPlaybackRate) ? Math.Round(Math.Clamp(s.Speech.ReaderPlaybackRate, .5, 2), 1) : 1;
        s.AutoRead ??= new(); s.AutoRead.Speed = Choice(s.AutoRead.Speed, "normal", "normal", "fast", "instant"); s.Startup ??= new(); s.Window ??= new();
        s.Window.Width = Math.Clamp(s.Window.Width, 480, 10000); s.Window.Height = Math.Clamp(s.Window.Height, 420, 10000); s.Window.State = Choice(s.Window.State, "normal", "normal", "maximized");
        s.Window.X = s.Window.X is int x ? Math.Clamp(x, -100000, 100000) : null; s.Window.Y = s.Window.Y is int y ? Math.Clamp(y, -100000, 100000) : null;
        s.Hotkeys ??= new(); var h = s.Hotkeys; h.Fixed = CleanKey(h.Fixed, "Alt+Z"); h.Snippet = CleanKey(h.Snippet, "Alt+S"); h.ReadAgain = CleanKey(h.ReadAgain, ""); h.AutoRead = CleanKey(h.AutoRead, "");
        s.Ocr ??= new(); s.Ocr.RecognitionMode = Choice(s.Ocr.RecognitionMode, "standard", "standard", "enhanced"); s.Ocr.Strength = Choice(s.Ocr.Strength, "conservative", "conservative", "balanced", "strong");
        s.Ocr.Replacements = (s.Ocr.Replacements ?? []).Where(r => r != null && !string.IsNullOrWhiteSpace(r.Original)).Take(250).ToList();
        foreach (var rule in s.Ocr.Replacements) { rule.Original = rule.Original.Trim()[..Math.Min(rule.Original.Trim().Length, 200)]; rule.Replacement ??= ""; rule.Replacement = rule.Replacement[..Math.Min(rule.Replacement.Length, 500)]; }
        s.Ocr.Replacements = s.Ocr.Replacements.DistinctBy(r => (r.CaseSensitive ? r.Original : r.Original.ToUpperInvariant(), r.CaseSensitive, r.WholeWord)).ToList();
        s.Ocr.ProtectedWords = (s.Ocr.ProtectedWords ?? []).Where(w => !string.IsNullOrWhiteSpace(w)).Select(w => w.Trim()[..Math.Min(w.Trim().Length, 200)]).Distinct(StringComparer.OrdinalIgnoreCase).Take(500).ToList();
        var profiles = new List<CaptureProfile>(); var ids = new HashSet<string>(); var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var p in (s.Profiles ?? []).Take(100))
        {
            if (p == null || string.IsNullOrWhiteSpace(p.Id)) continue;
            p.Id = p.Id.Trim()[..Math.Min(p.Id.Trim().Length, 80)]; if (!ids.Add(p.Id)) continue;
            p.Name = string.IsNullOrWhiteSpace(p.Name) ? "Profile" : p.Name.Trim()[..Math.Min(p.Name.Trim().Length, 80)];
            var original = p.Name; var n = 2; while (!names.Add(p.Name)) p.Name = $"{original} ({n++})";
            if (!CaptureGeometry.Valid(p.Area?.Box)) p.Area = null;
            else if (p.Area!.RelativeBox is not { Length: 4 } rel || rel.Any(v => !double.IsFinite(v) || v < -10 || v > 10) || rel[2] <= rel[0] || rel[3] <= rel[1] || string.IsNullOrWhiteSpace(p.Area.MonitorDevice) || !CaptureGeometry.Valid(p.Area.MonitorBounds)) { p.Area.MonitorDevice = ""; p.Area.MonitorBounds = null; p.Area.MonitorDpi = null; p.Area.RelativeBox = null; }
            else { p.Area.MonitorDpi = p.Area.MonitorDpi is { Length: 2 } dpi ? dpi.Select(d => Math.Clamp(d, 48, 960)).ToArray() : [96, 96]; }
            p.Settings ??= new(); profiles.Add(p);
        }
        if (profiles.Count == 0) profiles.Add(new() { Area = CaptureGeometry.Valid(s.FixedBox) ? new() { Box = s.FixedBox! } : null });
        s.Profiles = profiles; if (!profiles.Any(p => p.Id == s.SelectedProfileId)) s.SelectedProfileId = profiles[0].Id;
        s.FixedBox = s.SelectedProfile.Area?.Box; return s;
    }
    private static string CleanKey(string? key, string fallback) => key == "" ? "" : string.IsNullOrWhiteSpace(key) ? fallback : key.Trim();
}

public sealed class ConfigStore
{
    public static string DefaultPath => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "GameTextReader", "config.json");
    private readonly object gate = new(); private bool backedUp; private bool loadFailed, importPendingReload;
    public string PathName { get; }
    public AppSettings Current { get; private set; } = SettingsCodec.Normalize(new());
    public ConfigStore(string? path = null) => PathName = path ?? DefaultPath;
    public AppSettings Load(IEnumerable<string>? legacyPaths = null)
    {
        lock (gate)
        {
            if (File.Exists(PathName)) { try { Current = SettingsCodec.Read(File.ReadAllText(PathName)); } catch { loadFailed = true; throw; } }
            else foreach (var path in legacyPaths ?? [])
            {
                try { Current = ReadImport(path); Save(Current); break; } catch (Exception e) when (e is IOException or UnauthorizedAccessException or JsonException or InvalidDataException) { }
            }
            importPendingReload = false; return Current.Clone();
        }
    }
    public void Save(AppSettings settings)
    {
        lock (gate)
        {
            if (loadFailed) throw new InvalidDataException("The existing settings file could not be read. Import a valid backup before saving; the original is preserved.");
            if (importPendingReload) throw new InvalidOperationException("Settings were imported. Restart the app before making more changes; the imported file is preserved.");
            var json = SettingsCodec.Write(settings); Directory.CreateDirectory(Path.GetDirectoryName(PathName)!);
            if (!backedUp && File.Exists(PathName)) { File.Copy(PathName, UniqueBackup("config.before-csharp")); backedUp = true; }
            var temp = PathName + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try { File.WriteAllText(temp, json); File.Move(temp, PathName, true); Current = SettingsCodec.Read(json); }
            finally { if (File.Exists(temp)) File.Delete(temp); }
        }
    }
    public void Import(string folder)
    {
        var path = new[] { Path.Combine(folder, "config.json"), Path.Combine(folder, "_internal", "config.json") }.FirstOrDefault(File.Exists) ?? throw new FileNotFoundException("No config.json was found in this app folder.");
        var imported = ReadImport(path);
        if (File.Exists(PathName)) File.Copy(PathName, UniqueBackup("config.before-import"));
        var previousFailure = loadFailed; var previousImport = importPendingReload; loadFailed = false; importPendingReload = false;
        try { Save(imported); importPendingReload = true; } catch { loadFailed = previousFailure; importPendingReload = previousImport; throw; }
    }
    private static AppSettings ReadImport(string path)
    {
        var json = File.ReadAllText(path); var node = JsonNode.Parse(json) as JsonObject ?? throw new InvalidDataException("Settings must be a JSON object.");
        if (!node.Any(p => new[] { "theme", "voice", "ocr", "capture_profiles", "fixed_box", "hotkeys", "speech" }.Contains(p.Key))) throw new InvalidDataException("This is not a Game Text Reader settings file.");
        return SettingsCodec.Read(json);
    }
    private string UniqueBackup(string name)
    {
        var directory = Path.GetDirectoryName(PathName)!; var path = Path.Combine(directory, name + ".json"); var n = 1;
        while (File.Exists(path)) path = Path.Combine(directory, $"{name}.{n++}.json"); return path;
    }
}
