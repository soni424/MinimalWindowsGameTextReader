using System.IO;
using Microsoft.Web.WebView2.Core;
using TrayIcon = System.Windows.Forms.NotifyIcon;

namespace GameTextReader.App;

// Native availability is a system boundary: tests can simulate a missing runtime
// or tray without uninstalling software or changing the user's Windows settings.
internal sealed class DesktopServices
{
    internal Func<string, Task<CoreWebView2Environment>> CreatePreviewEnvironment { get; init; } = folder => CoreWebView2Environment.CreateAsync(null, folder);
    internal string PreviewAssetDirectory { get; init; } = Path.Combine(AppContext.BaseDirectory, "Assets", "Preview");
    internal Func<TrayIcon> CreateTrayIcon { get; init; } = () => new TrayIcon { Icon = new System.Drawing.Icon(Path.Combine(AppContext.BaseDirectory, "assets", "app_icon.ico")), Text = "Game Text Reader", Visible = true };
    internal GameTextReader.Windows.AutoReadEnvironment? AutoReadEnvironment { get; init; }
}
