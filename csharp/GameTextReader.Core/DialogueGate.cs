using System.Text.RegularExpressions;

namespace GameTextReader.Core;

public sealed record AutoReadPolicy(string Speed, int PollMs, int SettleMs, int ConfirmMs, int IdleMs, int Confirmations = 2, int GrowthMs = 0)
{
    public static AutoReadPolicy For(string speed) => speed switch { "fast" => new("fast", 120, 180, 120, 1000), "instant" => new("instant", 60, 60, 60, 750, 1, 250), _ => new("normal", 350, 500, 400, 2000) };
}
public enum DialogueDecision { Ignore, Speak, CancelGrowth }
public sealed class DialogueGate
{
    public string Candidate { get; private set; } = ""; public int Confirmations { get; private set; }
    public string LastSpoken { get; private set; } = ""; public long CandidateSince { get; private set; }
    public bool Recovering { get; private set; }
    private string growthOrigin = "";
    public static string Normalize(string value) => Regex.Replace(value, @"\s+", " ").Trim().ToLowerInvariant();
    public void ResetCandidate() { Candidate = ""; Confirmations = 0; Recovering = false; growthOrigin = ""; }
    public void RememberManual(string text) { LastSpoken = Normalize(text); ResetCandidate(); }
    public static bool Similar(string left, string right)
    {
        if (left.Length == 0 || right.Length == 0) return false;
        // Ratcliff/Obershelp matching blocks, matching Python's SequenceMatcher(autoJunk=False).
        // An exhausted work budget takes the conservative confirmation path.
        if (2d * Math.Min(left.Length, right.Length) / (left.Length + right.Length) < .85) return false;
        var positions = new Dictionary<char, List<int>>(); for (int j = 0; j < right.Length; j++) { if (!positions.TryGetValue(right[j], out var list)) positions[right[j]] = list = []; list.Add(j); }
        var ranges = new Stack<(int a0, int a1, int b0, int b1)>(); ranges.Push((0, left.Length, 0, right.Length)); int total = 0, work = 0;
        while (ranges.TryPop(out var range))
        {
            int bestA = range.a0, bestB = range.b0, size = 0; var previous = new Dictionary<int, int>();
            for (int i = range.a0; i < range.a1; i++)
            {
                var current = new Dictionary<int, int>(); if (positions.TryGetValue(left[i], out var indices)) foreach (var j in indices) { if (j < range.b0) continue; if (j >= range.b1) break; if (++work > 500000) return true; var length = previous.GetValueOrDefault(j - 1) + 1; current[j] = length; if (length > size) { bestA = i - length + 1; bestB = j - length + 1; size = length; } }
                previous = current;
            }
            if (size == 0) continue; total += size; if (range.a0 < bestA && range.b0 < bestB) ranges.Push((range.a0, bestA, range.b0, bestB)); if (bestA + size < range.a1 && bestB + size < range.b1) ranges.Push((bestA + size, range.a1, bestB + size, range.b1));
        }
        return 2d * total / (left.Length + right.Length) >= .85;
    }
    public DialogueDecision Observe(string text, AutoReadPolicy policy, long now)
    {
        var value = Normalize(text);
        if (value != Candidate) { Candidate = value; Confirmations = 1; CandidateSince = now; } else Confirmations++;
        if (value.Length == 0) { if (Confirmations >= 2) { LastSpoken = ""; Recovering = false; growthOrigin = ""; } return DialogueDecision.Ignore; }
        if (policy.Speed != "instant") { if (Confirmations < policy.Confirmations || value == LastSpoken) return DialogueDecision.Ignore; LastSpoken = value; return DialogueDecision.Speak; }
        if (!Recovering && LastSpoken.Length > 0 && value.Length > LastSpoken.Length && value.StartsWith(LastSpoken, StringComparison.Ordinal)) { Recovering = true; growthOrigin = LastSpoken; return DialogueDecision.CancelGrowth; }
        if (Recovering)
        {
            bool related = value.StartsWith(growthOrigin, StringComparison.Ordinal) || Similar(growthOrigin, value);
            if (!related) { Recovering = false; growthOrigin = ""; }
            else if (Confirmations >= 2 && now - CandidateSince >= policy.GrowthMs) { LastSpoken = value; Recovering = false; growthOrigin = ""; return DialogueDecision.Speak; }
            else return DialogueDecision.Ignore;
        }
        if (value == LastSpoken) return DialogueDecision.Ignore;
        if ((value.Count(char.IsLetterOrDigit) < 2 || Similar(LastSpoken, value)) && Confirmations < 2) return DialogueDecision.Ignore;
        LastSpoken = value; return DialogueDecision.Speak;
    }
    public static int EditDistance(string a, string b)
    {
        var previous = Enumerable.Range(0, b.Length + 1).ToArray();
        for (int i = 1; i <= a.Length; i++) { var current = new int[b.Length + 1]; current[0] = i; for (int j = 1; j <= b.Length; j++) current[j] = Math.Min(Math.Min(previous[j] + 1, current[j - 1] + 1), previous[j - 1] + (a[i - 1] == b[j - 1] ? 0 : 1)); previous = current; }
        return previous[^1];
    }
}
