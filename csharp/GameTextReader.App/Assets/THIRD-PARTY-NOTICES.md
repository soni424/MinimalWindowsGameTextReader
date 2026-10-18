# Third-party components

The C# port uses the pinned packages recorded in its package lock files. Their upstream notices and licenses apply:

- .NET/WPF and System.Speech — Microsoft, MIT: https://github.com/dotnet/runtime/blob/main/LICENSE.TXT and https://github.com/dotnet/wpf/blob/main/LICENSE.TXT
- Microsoft Edge WebView2 SDK — Microsoft: https://www.nuget.org/packages/Microsoft.Web.WebView2
- AvalonEdit — icsharpcode, MIT: https://github.com/icsharpcode/AvalonEdit/blob/master/LICENSE
- Markdig — Alexandre Mutel, BSD-2-Clause: https://github.com/xoofx/markdig/blob/main/license.txt
- HtmlSanitizer — Michael Ganss, MIT: https://github.com/mganss/HtmlSanitizer/blob/master/LICENSE
- AngleSharp — AngleSharp contributors, MIT: https://github.com/AngleSharp/AngleSharp/blob/devel/LICENSE
- SymSpell — Wolf Garbe, MIT. The dictionary files are the same resources used by the Python application; see `Assets/Dictionaries/SYMSpell-LICENSE-README.md` and `Assets/Licenses`.
- KaTeX — MIT. The existing local preview assets include `assets/markdown_preview/KATEX_LICENSE.txt` and the bundled fonts.

Test-only packages NUnit, NUnit3TestAdapter and Microsoft.NET.Test.Sdk are not shipped as runtime dependencies.
