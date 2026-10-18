using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using GameTextReader.Core;
using GameTextReader.Windows;

namespace GameTextReader.App;

public static class Dialogs
{
    private static Window Create(Window owner, string title, double width = 560, double height = 330) => new() { Owner = owner, Title = title, Width = width, Height = height, WindowStartupLocation = WindowStartupLocation.CenterOwner, ShowInTaskbar = false, MinWidth = 380, MinHeight = 240 };
    public static void Info(Window owner, string title, string text)
    {
        var window = Create(owner, title, 640, 380); var panel = new DockPanel { Margin = new Thickness(22) }; var close = new Button { Content = "Close", IsDefault = true, IsCancel = true, HorizontalAlignment = HorizontalAlignment.Right }; DockPanel.SetDock(close, Dock.Bottom); close.Click += (_, _) => window.Close(); panel.Children.Add(close); panel.Children.Add(new TextBox { Text = text, IsReadOnly = true, TextWrapping = TextWrapping.Wrap, VerticalScrollBarVisibility = ScrollBarVisibility.Auto }); window.Content = panel; window.ShowDialog();
    }
    public static bool Confirm(Window owner, string title, string text)
    {
        var window = Create(owner, title, 520, 250); var panel = new DockPanel { Margin = new Thickness(22) }; var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right }; var cancel = new Button { Content = "Cancel", IsCancel = true }; var accept = new Button { Content = "Confirm", IsDefault = true }; accept.Click += (_, _) => window.DialogResult = true; buttons.Children.Add(cancel); buttons.Children.Add(accept); DockPanel.SetDock(buttons, Dock.Bottom); panel.Children.Add(buttons); panel.Children.Add(new TextBlock { Text = text, TextWrapping = TextWrapping.Wrap }); window.Content = panel; return window.ShowDialog() == true;
    }
    public static string? Prompt(Window owner, string title, string label, string initial = "")
    {
        var window = Create(owner, title, 500, 250); var panel = new StackPanel { Margin = new Thickness(22) }; panel.Children.Add(new TextBlock { Text = label }); var entry = new TextBox { Text = initial, Margin = new Thickness(0, 12, 0, 18) }; panel.Children.Add(entry); var row = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right }; var cancel = new Button { Content = "Cancel", IsCancel = true }; var save = new Button { Content = "Save", IsDefault = true }; save.Click += (_, _) => { if (entry.Text.Trim().Length > 0) window.DialogResult = true; }; row.Children.Add(cancel); row.Children.Add(save); panel.Children.Add(row); window.Content = panel; window.Loaded += (_, _) => { entry.Focus(); entry.SelectAll(); }; return window.ShowDialog() == true ? entry.Text : null;
    }
    public static ReplacementRule? Replacement(MainWindow owner, ReplacementRule? existing, string original, Action<string> preview)
    {
        var window = Create(owner, "Replacement rule", 680, 350); var panel = new StackPanel { Margin = new Thickness(22) };
        TextBox Field(string label, string value)
        {
            panel.Children.Add(new TextBlock { Text = label, Margin = new Thickness(3, 0, 0, 4) }); var row = new DockPanel(); var play = new Button { Content = "Play", Tag = "play", MinWidth = 84 }; DockPanel.SetDock(play, Dock.Right); row.Children.Add(play); var entry = new TextBox { Text = value, MinWidth = 350 }; row.Children.Add(entry); panel.Children.Add(row); play.IsEnabled = !string.IsNullOrWhiteSpace(value); entry.TextChanged += (_, _) => play.IsEnabled = !string.IsNullOrWhiteSpace(entry.Text); play.Click += (_, _) => preview(entry.Text); return entry;
        }
        var from = Field("Text detected by OCR", existing?.Original ?? original); var to = Field("Replace it with", existing?.Replacement ?? "");
        var options = new WrapPanel { Margin = new Thickness(0, 12, 0, 10) }; var enabled = new CheckBox { Content = "Enabled", IsChecked = existing?.Enabled ?? true }; var whole = new CheckBox { Content = "Match whole words", IsChecked = existing?.WholeWord ?? true }; var exact = new CheckBox { Content = "Match exact case", IsChecked = existing?.CaseSensitive ?? false }; options.Children.Add(enabled); options.Children.Add(whole); options.Children.Add(exact); panel.Children.Add(options);
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right }; buttons.Children.Add(new Button { Content = "Cancel", IsCancel = true }); var save = new Button { Content = "Save rule", Tag = "save", IsDefault = true }; save.Click += (_, _) => { if (!string.IsNullOrWhiteSpace(from.Text)) window.DialogResult = true; }; buttons.Children.Add(save); panel.Children.Add(buttons); window.Content = panel; window.Loaded += (_, _) => { owner.DecorateButtons(window); if (from.Text.Length == 0) from.Focus(); else { to.Focus(); to.SelectAll(); } };
        return window.ShowDialog() == true ? new() { Original = from.Text.Trim(), Replacement = to.Text, Enabled = enabled.IsChecked == true, WholeWord = whole.IsChecked == true, CaseSensitive = exact.IsChecked == true } : null;
    }
    public static string? RecordShortcut(Window owner, string action)
    {
        var window = Create(owner, "Record shortcut", 520, 260); string? result = null; var panel = new StackPanel { Margin = new Thickness(24) }; panel.Children.Add(new TextBlock { Text = "Press the shortcut for “" + action + "”.", TextWrapping = TextWrapping.Wrap }); var status = new TextBlock { Margin = new Thickness(0, 20, 0, 20), TextWrapping = TextWrapping.Wrap }; panel.Children.Add(status); panel.Children.Add(new Button { Content = "Cancel", IsCancel = true }); window.Content = panel;
        window.PreviewKeyDown += (_, e) =>
        {
            var key = e.Key == Key.System ? e.SystemKey : e.Key; if (key is Key.LeftCtrl or Key.RightCtrl or Key.LeftAlt or Key.RightAlt or Key.LeftShift or Key.RightShift or Key.LWin or Key.RWin) return;
            var labels = new List<string>(); var modifiers = Keyboard.Modifiers; if (modifiers.HasFlag(ModifierKeys.Control)) labels.Add("Ctrl"); if (modifiers.HasFlag(ModifierKeys.Alt)) labels.Add("Alt"); if (modifiers.HasFlag(ModifierKeys.Shift)) labels.Add("Shift"); if (modifiers.HasFlag(ModifierKeys.Windows)) labels.Add("Win");
            var name = key switch { >= Key.A and <= Key.Z => key.ToString(), >= Key.D0 and <= Key.D9 => ((char)('0' + key - Key.D0)).ToString(), Key.Return => "Enter", Key.Escape => "Esc", Key.OemSemicolon => ";", Key.OemPlus => "=", Key.OemComma => ",", Key.OemMinus => "-", Key.OemPeriod => ".", Key.OemQuestion => "/", Key.OemTilde => "`", Key.OemOpenBrackets => "[", Key.OemPipe => "\\", Key.OemCloseBrackets => "]", Key.OemQuotes => "'", _ => key.ToString() }; labels.Add(name);
            try { result = HotkeyParser.Parse(string.Join('+', labels))!.Display; window.DialogResult = true; } catch (Exception error) { status.Text = error.Message; }
            e.Handled = true;
        };
        return window.ShowDialog() == true ? result : null;
    }
    public static void Corrections(MainWindow owner, CorrectionResult initial, Action<CorrectionResult> changed)
    {
        var current = initial; var window = Create(owner, "OCR correction details", 950, 750); var panel = new DockPanel { Margin = new Thickness(18) }; var close = new Button { Content = "Close", IsCancel = true, IsDefault = true, HorizontalAlignment = HorizontalAlignment.Right }; DockPanel.SetDock(close, Dock.Bottom); panel.Children.Add(close);
        var review = new StackPanel { Margin = new Thickness(0, 12, 0, 0) }; DockPanel.SetDock(review, Dock.Bottom); panel.Children.Add(review);
        review.Children.Add(new TextBlock { Text = "Needs review", FontSize = 18, FontWeight = FontWeights.SemiBold }); var list = new ListBox { Height = 130, DisplayMemberPath = "Original", Margin = new Thickness(0, 8, 0, 0) }; review.Children.Add(list); var explanation = new TextBlock { TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 8, 0, 0) }; review.Children.Add(explanation); var choices = new ComboBox { Margin = new Thickness(0, 5, 0, 0) }; review.Children.Add(choices); var actions = new StackPanel { Orientation = Orientation.Horizontal }; var apply = new Button { Content = "Apply", Tag = "check" }; var dismiss = new Button { Content = "Dismiss", Tag = "cancel" }; actions.Children.Add(apply); actions.Children.Add(dismiss); review.Children.Add(actions);
        var tabs = new TabControl(); var raw = new TextBox { Text = current.RawText, IsReadOnly = true, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, VerticalScrollBarVisibility = ScrollBarVisibility.Auto }; var corrected = new TextBox { IsReadOnly = true, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, VerticalScrollBarVisibility = ScrollBarVisibility.Auto }; var trace = new TextBox { IsReadOnly = true, TextWrapping = TextWrapping.Wrap, VerticalScrollBarVisibility = ScrollBarVisibility.Auto }; tabs.Items.Add(new TabItem { Header = "Raw OCR", Content = raw }); tabs.Items.Add(new TabItem { Header = "Corrected text", Content = corrected }); tabs.Items.Add(new TabItem { Header = "Change history", Content = trace }); panel.Children.Add(tabs);
        void Refresh() { corrected.Text = current.CorrectedText; trace.Text = string.Join("\n\n", current.Corrections.Select(c => $"{c.Original} → {c.Replacement}\n{c.Source}: {c.Reason}")); list.ItemsSource = current.Suggestions; list.SelectedIndex = current.Suggestions.Count > 0 ? 0 : -1; apply.IsEnabled = dismiss.IsEnabled = current.Suggestions.Count > 0; }
        list.SelectionChanged += (_, _) => { if (list.SelectedItem is CorrectionSuggestion suggestion) { explanation.Text = suggestion.Reason; choices.ItemsSource = suggestion.Alternatives; choices.SelectedIndex = 0; } else { explanation.Text = "No pending suggestions."; choices.ItemsSource = null; } };
        void Review(bool accept) { if (list.SelectedItem is not CorrectionSuggestion suggestion) return; try { current = current.Review(suggestion, accept ? choices.SelectedItem as string : null); changed(current); Refresh(); } catch (Exception error) { Info(window, "Suggestion unavailable", error.Message); } }
        apply.Click += (_, _) => Review(true); dismiss.Click += (_, _) => Review(false); Refresh(); window.Content = panel; window.Loaded += (_, _) => owner.DecorateButtons(window); window.ShowDialog();
    }
}
