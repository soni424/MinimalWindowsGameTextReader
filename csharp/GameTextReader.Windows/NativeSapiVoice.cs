using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using GameTextReader.Core;

namespace GameTextReader.Windows;

// STA-owned adapter. Read raw SAPI events instead of System.Speech's dispatch
// layer, which can substring an invalid native word length on a ThreadPool thread.
internal sealed class NativeSapiVoice : IDisposable
{
    internal sealed record Word(double Seconds, int Start, int End);
    internal sealed record Result(byte[] Wave, IReadOnlyList<Word> Words);
    private readonly object speaker;
    private readonly ISpEventSource events;
    private readonly int ownerThread = Environment.CurrentManagedThreadId;
    private long generation;
    private bool disposed;
    private const ulong Interests = (1UL << 30) | (1UL << 33) | (1UL << 2) | (1UL << 5);

    internal NativeSapiVoice(string id)
    {
        speaker = Create("SAPI.SpVoice");
        try
        {
            events = (ISpEventSource)speaker;
            object tokens = ((dynamic)speaker).GetVoices();
            try
            {
                var found = false;
                for (int i = 0; i < (int)((dynamic)tokens).Count; i++)
                {
                    object token = ((dynamic)tokens).Item(i);
                    try
                    {
                        var tokenId = (string)((dynamic)token).Id;
                        if (!tokenId.Equals(id, StringComparison.OrdinalIgnoreCase) && !tokenId.EndsWith("\\" + id, StringComparison.OrdinalIgnoreCase)) continue;
                        ((dynamic)speaker).Voice = token;
                        found = true;
                        break;
                    }
                    finally { Release(token); }
                }
                if (!found) throw new InvalidOperationException("The selected SAPI voice is unavailable. Choose another voice.");
            }
            finally { Release(tokens); }
            Marshal.ThrowExceptionForHR(events.SetInterest(Interests, Interests));
        }
        catch { Release(speaker); throw; }
    }

    internal async Task<Result> SynthesizeAsync(SynthesisInput input, int rate, double volume, CancellationToken cancellation)
    {
        CheckThread(); ObjectDisposedException.ThrowIf(disposed, this);
        var current = ++generation;
        object? stream = null, format = null, waveFormat = null;
        try
        {
            cancellation.ThrowIfCancellationRequested();
            stream = Create("SAPI.SpMemoryStream"); format = Create("SAPI.SpAudioFormat");
            ((dynamic)format).Type = 22; // SAFT22kHz16BitMono; same PCM format as the Python backend.
            ((dynamic)stream).Format = format;
            ((dynamic)speaker).AllowAudioOutputFormatChangesOnNextSet = false;
            ((dynamic)speaker).AudioOutputStream = stream;
            ((dynamic)speaker).Rate = Math.Clamp(rate, -10, 10);
            ((dynamic)speaker).Volume = (int)Math.Clamp(volume, 0, 100);
            Drain();
            var streamId = unchecked((uint)(int)((dynamic)speaker).Speak(input.Text, 1 | (input.Structured ? 8 | 128 : 16)));
            var raw = new List<SapiEvent>();
            var watch = Stopwatch.StartNew();
            var deadline = TimeSpan.FromSeconds(Math.Clamp(input.Text.Length / 20d, 30, 300));
            var complete = false;
            while (!complete)
            {
                cancellation.ThrowIfCancellationRequested();
                if (current != generation) throw new OperationCanceledException("The synthesis revision was replaced.");
                foreach (var entry in Drain())
                {
                    if (entry.Stream != streamId) continue;
                    if (entry.Id == 5) raw.Add(entry);
                    else if (entry.Id == 2) complete = true;
                }
                if (complete) break;
                if (watch.Elapsed > deadline) throw new TimeoutException("The selected SAPI voice did not finish preparing speech.");
                await Task.Delay(10); // Keeps the STA message loop available for Stop, seek, and other requests.
            }
            object status = ((dynamic)speaker).Status;
            try { Marshal.ThrowExceptionForHR((int)((dynamic)status).LastHResult); }
            finally { Release(status); }
            var pcm = (byte[])((dynamic)stream).GetData();
            waveFormat = ((dynamic)format).GetWaveFormatEx();
            var samples = (int)((dynamic)waveFormat).SamplesPerSec;
            var channels = (short)((dynamic)waveFormat).Channels;
            var bits = (short)((dynamic)waveFormat).BitsPerSample;
            if ((int)((dynamic)waveFormat).FormatTag != 1 || samples <= 0 || channels <= 0 || bits <= 0 || bits % 8 != 0 || pcm.Length == 0)
                throw new InvalidOperationException("SAPI returned no usable PCM audio.");
            var bytesPerSecond = checked(samples * channels * (bits / 8));
            if (pcm.Length % (channels * (bits / 8)) != 0) throw new InvalidOperationException("SAPI returned incomplete audio frames.");
            var words = new List<Word>(raw.Count);
            foreach (var entry in raw)
                if (TryWord(entry.Stream, streamId, entry.AudioOffset, (long)entry.LParam, (ulong)entry.WParam, input.Text.Length, bytesPerSecond, pcm.Length, out var word)) words.Add(word!);
            using var output = new MemoryStream(pcm.Length + 44);
            using (var writer = new BinaryWriter(output, Encoding.ASCII, true))
            {
                writer.Write("RIFF"u8); writer.Write(checked(pcm.Length + 36)); writer.Write("WAVEfmt "u8); writer.Write(16);
                writer.Write((short)1); writer.Write(channels); writer.Write(samples); writer.Write(bytesPerSecond);
                writer.Write((short)(channels * (bits / 8))); writer.Write(bits); writer.Write("data"u8); writer.Write(pcm.Length); writer.Write(pcm);
            }
            return new(output.ToArray(), words);
        }
        catch { Cancel(); throw; }
        finally
        {
            // The stream is never switched to a physical output before synthesis.
            try { ((dynamic)speaker).AudioOutputStream = null; }
            finally { Release(waveFormat); Release(format); Release(stream); }
        }
    }

    internal static bool TryWord(uint stream, uint expectedStream, ulong audioOffset, long start, ulong length, int inputLength, int bytesPerSecond, int audioLength, out Word? word)
    {
        word = null;
        if (stream != expectedStream || start < 0 || start >= inputLength || length == 0 || length > (ulong)(inputLength - start) || bytesPerSecond <= 0 || audioLength <= 0 || audioOffset >= (ulong)audioLength) return false;
        word = new(audioOffset / (double)bytesPerSecond, (int)start, (int)(start + (long)length));
        return true;
    }
    private List<SapiEvent> Drain()
    {
        var result = new List<SapiEvent>(); var batch = new SapiEvent[64]; uint count;
        do
        {
            Marshal.ThrowExceptionForHR(events.GetEvents((uint)batch.Length, batch, out count));
            for (int i = 0; i < count; i++)
            {
                var entry = batch[i];
                // Only word/end events are requested. Still free unexpected COM-owned payloads safely.
                if (entry.ParameterType is 1 or 2) { if (entry.LParam != 0) Marshal.Release(entry.LParam); }
                else if (entry.ParameterType is 3 or 4) { if (entry.LParam != 0) Marshal.FreeCoTaskMem(entry.LParam); }
                else result.Add(entry);
            }
        } while (count == batch.Length);
        return result;
    }
    internal void Cancel()
    {
        CheckThread(); if (disposed) return; generation++;
        ((dynamic)speaker).Speak("", 1 | 2 | 16);
    }
    private void CheckThread() { if (Environment.CurrentManagedThreadId != ownerThread) throw new InvalidOperationException("SAPI must remain on its owning speech thread."); }
    private static object Create(string name) => Activator.CreateInstance(Type.GetTypeFromProgID(name) ?? throw new PlatformNotSupportedException("Windows SAPI is unavailable."))!;
    private static void Release(object? value) { if (value != null && Marshal.IsComObject(value)) Marshal.ReleaseComObject(value); }
    public void Dispose() { CheckThread(); if (disposed) return; try { Cancel(); } finally { disposed = true; Release(speaker); } }

    [StructLayout(LayoutKind.Sequential)]
    private struct SapiEvent
    {
        public ushort Id, ParameterType;
        public uint Stream;
        public ulong AudioOffset;
        public nuint WParam;
        public nint LParam;
    }
    // Exact vtable order from sapi.h: ISpNotifySource followed by ISpEventSource.
    [ComImport, Guid("BE7A9CCE-5F9E-11D2-960F-00C04F8EE628"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface ISpEventSource
    {
        [PreserveSig] int SetNotifySink(nint sink);
        [PreserveSig] int SetNotifyWindowMessage(nint window, uint message, nuint wParam, nint lParam);
        [PreserveSig] int SetNotifyCallbackFunction(nint callback, nuint wParam, nint lParam);
        [PreserveSig] int SetNotifyCallbackInterface(nint callback, nuint wParam, nint lParam);
        [PreserveSig] int SetNotifyWin32Event();
        [PreserveSig] int WaitForNotifyEvent(uint milliseconds);
        [PreserveSig] nint GetNotifyEventHandle();
        [PreserveSig] int SetInterest(ulong interest, ulong queuedInterest);
        [PreserveSig] int GetEvents(uint count, [Out, MarshalAs(UnmanagedType.LPArray, SizeParamIndex = 0)] SapiEvent[] buffer, out uint fetched);
        [PreserveSig] int GetInfo(nint info);
    }
}
