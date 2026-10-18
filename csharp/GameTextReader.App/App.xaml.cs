using System.IO;
using System.Windows;

namespace GameTextReader.App;

public partial class App : Application
{
    private readonly bool initializeServices;
    public App() : this(true) { }
    internal App(bool initializeServices) => this.initializeServices = initializeServices;
    protected override void OnStartup(StartupEventArgs e)
    {
        if (!initializeServices) return;
        base.OnStartup(e);
        string? settings = null; var index = Array.IndexOf(e.Args, "--settings"); if (index >= 0 && index + 1 < e.Args.Length) settings = Path.GetFullPath(e.Args[index + 1]);
        var smokeIndex = Array.IndexOf(e.Args, "--smoke-report"); string? report = smokeIndex >= 0 && smokeIndex + 1 < e.Args.Length ? Path.GetFullPath(e.Args[smokeIndex + 1]) : null;
        var probeIndex = Array.IndexOf(e.Args, "--speech-probe");
        if (probeIndex >= 0)
        {
            var reportIndex = Array.IndexOf(e.Args, "--probe-report");
            if (settings == null || settings.Equals(Core.ConfigStore.DefaultPath, StringComparison.OrdinalIgnoreCase) || probeIndex + 1 >= e.Args.Length || reportIndex < 0 || reportIndex + 1 >= e.Args.Length)
                throw new InvalidOperationException("Speech verification requires isolated settings, input, and report paths.");
            var probe = new MainWindow(settings, true); MainWindow = probe;
            Dispatcher.BeginInvoke(() => probe.RunSpeechProbe(Path.GetFullPath(e.Args[probeIndex + 1]), Path.GetFullPath(e.Args[reportIndex + 1])));
            return;
        }
        DispatcherUnhandledException += (_, error) => { MessageBox.Show(error.Exception.Message, "Game Text Reader", MessageBoxButton.OK, MessageBoxImage.Error); error.Handled = true; };
        if (report != null && (settings == null || settings.Equals(Core.ConfigStore.DefaultPath, StringComparison.OrdinalIgnoreCase))) throw new InvalidOperationException("Package verification requires an isolated --settings path.");
        var window = new MainWindow(settings, e.Args.Contains("--no-integrations")); MainWindow = window;
        if (!e.Args.Contains("--startup")) window.Show(); else window.LoadedFromStartup = true;
        window.InitializeServices(e.Args.Contains("--startup"));
        if (report != null) Dispatcher.BeginInvoke(() => window.RunPackageSmoke(report));
    }
}
