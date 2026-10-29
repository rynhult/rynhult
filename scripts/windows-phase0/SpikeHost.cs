// Safety sign-off: standard-account Session 0 qualification only. No user commands, provider logins or TCP listener.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Management;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.ServiceProcess;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using Microsoft.Win32;
using Microsoft.Win32.SafeHandles;

namespace Rynhult.Spike
{
    public sealed class ProbeConfig
    {
        public string InstallId, InstallUtc, AgentSid, OwnerSid, ProfilePath, ServiceName, PipeName;
        public string[] Rights;
    }

    sealed class SpikeService : ServiceBase
    {
        const string Root = @"C:\RynhultSpike";
        const uint FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000;
        const uint PIPE_REJECT_REMOTE_CLIENTS = 0x00000008;
        readonly ManualResetEvent stopping = new ManualResetEvent(false);
        readonly object evidenceLock = new object();
        readonly JavaScriptSerializer json = new JavaScriptSerializer();
        Thread worker, listener;
        NamedPipeServerStream server;
        ProbeConfig config;
        DateTime bootUtc, startedUtc;
        int pid;

        static void Main() { ServiceBase.Run(new SpikeService()); }
        SpikeService() { ServiceName = "RynhultSpike"; AutoLog = false; CanStop = true; }
        protected override void OnStart(string[] arguments)
        {
            worker = new Thread(Run) { IsBackground = true, Name = "qualification" };
            worker.Start();
        }
        protected override void OnStop()
        {
            stopping.Set();
            if (server != null) server.Dispose();
            if (worker != null && !worker.Join(15000)) Environment.FailFast("Probe did not stop within 15 seconds");
            if (listener != null && !listener.Join(5000)) Environment.FailFast("Pipe listener did not stop");
        }
        void Write(string name, object value, bool once)
        {
            lock (evidenceLock)
            {
                string path = Path.Combine(Root, "evidence", name);
                if (once && File.Exists(path)) return;
                string temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
                File.WriteAllText(temporary, json.Serialize(value), new UTF8Encoding(false));
                if (File.Exists(path)) File.Replace(temporary, path, null);
                else File.Move(temporary, path);
            }
        }
        void Fatal(Exception error)
        {
            Write("fatal.json", new { installId = config == null ? null : config.InstallId,
                utc = DateTime.UtcNow.ToString("o"), error = error.GetType().Name, detail = error.Message }, false);
            Environment.Exit(1); // Closing this process's Job handle kills only its own probe children.
        }
        void Run()
        {
            try
            {
                config = json.Deserialize<ProbeConfig>(File.ReadAllText(Path.Combine(Root, "config.json")));
                Guid.ParseExact(config.InstallId, "D");
                new SecurityIdentifier(config.AgentSid);
                new SecurityIdentifier(config.OwnerSid);
                if (WindowsIdentity.GetCurrent().User.Value != config.AgentSid ||
                    config.PipeName != "RynhultSpike." + config.InstallId)
                    throw new InvalidOperationException("Service identity/configuration mismatch");
                pid = Process.GetCurrentProcess().Id;
                startedUtc = DateTime.UtcNow;
                using (ManagementObjectSearcher query = new ManagementObjectSearcher("SELECT LastBootUpTime FROM Win32_OperatingSystem"))
                using (ManagementObjectCollection rows = query.Get())
                    foreach (ManagementObject row in rows)
                        bootUtc = ManagementDateTimeConverter.ToDateTime((string)row["LastBootUpTime"]).ToUniversalTime();
                if (bootUtc == default(DateTime)) throw new InvalidOperationException("Cannot establish boot time");
                listener = new Thread(Listen) { IsBackground = true, Name = "local-pipe" };
                listener.Start();
                // A service-side positive control uses the same identification-only client as Collect.
                string reply = Native.QueryPipe(config.PipeName, (uint)pid);
                Dictionary<string, object> positive = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(reply);
                if (!(bool)positive["accepted"] || (string)positive["clientSid"] != config.AgentSid)
                    throw new InvalidOperationException("Same-SID pipe control was refused");
                using (ConPtySession console = new ConPtySession(config.ProfilePath))
                {
                    if (stopping.WaitOne(10000)) return;
                    bool first = true;
                    do
                    {
                        bool alive = console.Alive;
                        string output = console.Output;
                        bool passed = alive && output.Contains("RYNHULT_CONPTY_OK") && output.Contains("127.0.0.1") && console.ReadError == null;
                        Write("conpty.json", new { installId = config.InstallId, utc = DateTime.UtcNow.ToString("o"),
                            childPid = console.ChildPid, childAlive = alive, observedSeconds = console.ObservedSeconds,
                            output = output, readerError = console.ReadError,
                            passed = passed
                        }, false);
                        if (first)
                        {
                            object sessions = null; string sessionError = null;
                            try { sessions = InteractiveSessions(); }
                            catch (Win32Exception error) { sessionError = error.Message; }
                            Write("boot-" + bootUtc.ToString("yyyyMMddTHHmmssZ") + ".json", new {
                                installId = config.InstallId, bootUtc = bootUtc.ToString("o"),
                                startedUtc = startedUtc.ToString("o"), proofUtc = DateTime.UtcNow.ToString("o"),
                                sessionId = Process.GetCurrentProcess().SessionId, servicePid = pid,
                                sessions = sessions, sessionQueryError = sessionError, sessionQuerySucceeded = sessionError == null,
                                observedSeconds = console.ObservedSeconds, conptyPassed = passed
                            }, true);
                            PruneBootRecords();
                            first = false;
                        }
                        using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
                        using (RegistryKey hive = Registry.Users.OpenSubKey(config.AgentSid))
                            Write("current.json", new { installId = config.InstallId,
                                utc = DateTime.UtcNow.ToString("o"), bootUtc = bootUtc.ToString("o"),
                                servicePid = pid, sessionId = Process.GetCurrentProcess().SessionId,
                                agentSid = identity.User.Value,
                                isAdmin = new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator),
                                profilePath = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                                profileEnvironment = Environment.GetEnvironmentVariable("USERPROFILE"),
                                profileHiveLoaded = hive != null, tokenPrivileges = TokenPrivileges(identity.Token)
                            }, false);
                        if (!alive) throw new InvalidOperationException("ConPTY child exited unexpectedly");
                    } while (!stopping.WaitOne(15000));
                }
            }
            catch (Exception error) { Fatal(error); }
        }
        void PruneBootRecords()
        {
            string[] records = Directory.GetFiles(Path.Combine(Root, "evidence"), "boot-*.json");
            Array.Sort(records, StringComparer.Ordinal);
            for (int i = 0; i < records.Length - 8; i++) File.Delete(records[i]);
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern SafePipeHandle CreateNamedPipeW(string name, uint access, uint mode, uint instances,
            uint output, uint input, uint timeout, ref Native.SecurityAttributes attributes);
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool GetNamedPipeClientProcessId(SafePipeHandle pipe, out uint pid);
        [DllImport("advapi32.dll", SetLastError = true)]
        static extern bool ImpersonateNamedPipeClient(SafePipeHandle pipe);
        [DllImport("advapi32.dll", SetLastError = true)] static extern bool RevertToSelf();
        void Listen()
        {
            try
            {
                string sddl = "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGW;;;" + config.AgentSid +
                    ")(A;;GRGW;;;" + config.OwnerSid + ")";
                Native.SecurityAttributes attributes = Native.Security(sddl);
                SafePipeHandle handle;
                try
                {
                    string name = @"\\.\pipe\" + config.PipeName;
                    uint access = 3 | 0x40000000 | FILE_FLAG_FIRST_PIPE_INSTANCE;
                    handle = CreateNamedPipeW(name, access, PIPE_REJECT_REMOTE_CLIENTS, 1, 4096, 4096, 1000, ref attributes);
                    if (handle.IsInvalid) throw new Win32Exception();
                    using (SafePipeHandle collision = CreateNamedPipeW(name, access, PIPE_REJECT_REMOTE_CLIENTS,
                               1, 4096, 4096, 1000, ref attributes))
                    {
                        int code = Marshal.GetLastWin32Error();
                        bool blocked = collision.IsInvalid && code == 5;
                        Write("pipe-policy.json", new { installId = config.InstallId, firstInstance = true,
                            localOnly = true, dacl = sddl, squattingBlocked = blocked, collisionError = code }, false);
                        if (!blocked) throw new InvalidOperationException("Second first-instance pipe did not fail with access denied");
                    }
                }
                finally { Native.LocalFree(attributes.Descriptor); }
                using (server = new NamedPipeServerStream(PipeDirection.InOut, true, false, handle))
                {
                    while (!stopping.WaitOne(0))
                    {
                        IAsyncResult pending = server.BeginWaitForConnection(null, null);
                        using (WaitHandle wait = pending.AsyncWaitHandle)
                        {
                            while (!wait.WaitOne(250)) if (stopping.WaitOne(0)) return;
                            server.EndWaitForConnection(pending);
                        }
                        byte[] request = new byte[1];
                        if (Native.Read(server, request, 0, 1) != 1 || request[0] != 0x50)
                            throw new InvalidDataException("Invalid probe request");
                        uint clientPid;
                        if (!GetNamedPipeClientProcessId(server.SafePipeHandle, out clientPid)) throw new Win32Exception();
                        if (!ImpersonateNamedPipeClient(server.SafePipeHandle)) throw new Win32Exception();
                        string clientSid;
                        try
                        {
                            using (WindowsIdentity identity = WindowsIdentity.GetCurrent(true))
                            {
                                if (identity == null || identity.User == null) throw new InvalidOperationException("No client token");
                                clientSid = identity.User.Value;
                            }
                        }
                        finally
                        {
                            // No file writes or dispatch while impersonating; failure to revert must terminate.
                            if (!RevertToSelf()) Environment.FailFast("Cannot revert pipe client identity");
                        }
                        bool accepted = clientSid == config.AgentSid;
                        object reply = new { installId = config.InstallId, utc = DateTime.UtcNow.ToString("o"),
                            accepted = accepted, clientSid = clientSid, clientPid = clientPid, servicePid = pid };
                        Write(clientSid == config.AgentSid ? "pipe-agent.json" :
                            clientSid == config.OwnerSid ? "pipe-owner.json" : "pipe-other.json", reply, false);
                        WriteReply(server, reply);
                        if (Native.Read(server, request, 0, 1) != 1 || request[0] != 0x41)
                            throw new InvalidDataException("Missing pipe response acknowledgement");
                        server.Disconnect(); // Retain the original first-instance handle for the entire service lifetime.
                    }
                }
            }
            catch (Exception error)
            {
                bool cancelled = stopping.WaitOne(0) && (error is ObjectDisposedException || error is IOException);
                if (!cancelled) Fatal(error);
            }
        }
        void WriteReply(NamedPipeServerStream pipe, object reply)
        {
            byte[] bytes;
            lock (evidenceLock) bytes = Encoding.UTF8.GetBytes(json.Serialize(reply) + "\n");
            pipe.Write(bytes, 0, bytes.Length);
            pipe.Flush();
        }

        [StructLayout(LayoutKind.Sequential)]
        struct Session { public uint Id; public IntPtr Name; public int State; }
        [DllImport("wtsapi32.dll", SetLastError = true)]
        static extern bool WTSEnumerateSessionsW(IntPtr server, uint reserved, uint version, out IntPtr sessions, out int count);
        [DllImport("wtsapi32.dll", SetLastError = true)]
        static extern bool WTSQuerySessionInformationW(IntPtr server, uint session, int kind, out IntPtr data, out uint bytes);
        [DllImport("wtsapi32.dll")] static extern void WTSFreeMemory(IntPtr memory);
        // Signed-in evidence from two sources; either one showing a user fails the gate. On windows-test-pc,
        // WTSEnumerateSessionsW fails for the standard probe account ("No more data is available"),
        // so the process snapshot (names and session ids need no access to the processes) is the
        // primary source. Its positive control: winlogon.exe must be visible in some non-zero session.
        static object InteractiveSessions()
        {
            Dictionary<int, bool> winlogon = new Dictionary<int, bool>();
            Dictionary<int, bool> shell = new Dictionary<int, bool>();
            foreach (Process process in Process.GetProcesses())
            {
                using (process)
                {
                    int id = process.SessionId;
                    if (id == 0) continue;
                    string name = process.ProcessName.ToLowerInvariant();
                    if (name == "winlogon") winlogon[id] = true;
                    if (name == "sihost" || name == "explorer" || name == "userinit") shell[id] = true;
                }
            }
            if (winlogon.Count == 0) throw new Win32Exception(1168, "No winlogon.exe visible outside Session 0; cannot tell who is signed in.");
            object wts = null; string wtsError = null;
            try { wts = WtsSessions(); }
            catch (Win32Exception error) { wtsError = error.NativeErrorCode + ": " + error.Message; }
            bool wtsUser = false;
            if (wts != null)
                foreach (object entry in (List<object>)wts)
                    if ((bool)entry.GetType().GetProperty("hasUser").GetValue(entry, null)) wtsUser = true;
            List<object> result = new List<object>();
            List<int> ids = new List<int>(winlogon.Keys);
            foreach (int id in shell.Keys) if (!ids.Contains(id)) ids.Add(id);
            ids.Sort();
            foreach (int id in ids)
                result.Add(new { sessionId = id, source = "processes", winlogon = winlogon.ContainsKey(id),
                    hasUser = shell.ContainsKey(id) });
            result.Add(new { sessionId = -1, source = "wts", wtsSessions = wts, wtsError = wtsError, hasUser = wtsUser });
            return result;
        }
        static object WtsSessions()
        {
            IntPtr buffer; int count;
            if (!WTSEnumerateSessionsW(IntPtr.Zero, 0, 1, out buffer, out count)) throw new Win32Exception();
            List<object> result = new List<object>();
            try
            {
                for (int i = 0; i < count; i++)
                {
                    Session session = (Session)Marshal.PtrToStructure(IntPtr.Add(buffer, i * Marshal.SizeOf(typeof(Session))), typeof(Session));
                    if (session.Id == 0) continue;
                    // The state decides: only Connected (1, the sign-in screen), Down, Init, Listen and Reset have no user.
                    // Active, Disconnected, Idle, Shadow and ConnectQuery count as signed in. The name is diagnostic only,
                    // because a standard account's name query can fail (windows-test-pc: "No more data is available").
                    bool noUserState = session.State == 1 || (session.State >= 6 && session.State <= 9);
                    IntPtr name; uint bytes; string userName = null; string nameError = null;
                    if (WTSQuerySessionInformationW(IntPtr.Zero, session.Id, 5, out name, out bytes))
                    {
                        try { userName = Marshal.PtrToStringUni(name); }
                        finally { WTSFreeMemory(name); }
                    }
                    else
                    {
                        int code = Marshal.GetLastWin32Error();
                        nameError = code + ": " + new Win32Exception(code).Message;
                    }
                    result.Add(new { sessionId = session.Id, state = session.State,
                        hasUser = !noUserState || !String.IsNullOrEmpty(userName), nameQueryError = nameError });
                }
                return result;
            }
            finally { WTSFreeMemory(buffer); }
        }
        [StructLayout(LayoutKind.Sequential)] struct Luid { public uint Low; public int High; }
        [StructLayout(LayoutKind.Sequential)] struct Privilege { public Luid Id; public uint Attributes; }
        [DllImport("advapi32.dll", SetLastError = true)]
        static extern bool GetTokenInformation(IntPtr token, int kind, IntPtr buffer, uint length, out uint needed);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool LookupPrivilegeNameW(string system, ref Luid id, StringBuilder name, ref uint length);
        static object TokenPrivileges(IntPtr token)
        {
            uint needed;
            GetTokenInformation(token, 3, IntPtr.Zero, 0, out needed);
            if (Marshal.GetLastWin32Error() != 122 || needed > 65536) throw new Win32Exception();
            IntPtr buffer = Marshal.AllocHGlobal((int)needed);
            try
            {
                if (!GetTokenInformation(token, 3, buffer, needed, out needed)) throw new Win32Exception();
                int count = Marshal.ReadInt32(buffer);
                List<object> privileges = new List<object>();
                for (int i = 0; i < count; i++)
                {
                    Privilege privilege = (Privilege)Marshal.PtrToStructure(
                        IntPtr.Add(buffer, 4 + i * Marshal.SizeOf(typeof(Privilege))), typeof(Privilege));
                    StringBuilder name = new StringBuilder(256); uint length = (uint)name.Capacity;
                    if (!LookupPrivilegeNameW(null, ref privilege.Id, name, ref length)) throw new Win32Exception();
                    privileges.Add(new { name = name.ToString(), enabled = (privilege.Attributes & 2) != 0 });
                }
                return privileges;
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }
    }

    sealed class ConPtySession : IDisposable
    {
        const uint CREATE_SUSPENDED = 4;
        const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000;
        const int PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016;
        IntPtr inputRead, inputWrite, outputRead, outputWrite, console, job, attributes;
        bool attributesReady;
        ProcessInfo child;
        readonly MemoryStream captured = new MemoryStream();
        readonly object outputLock = new object();
        readonly Stopwatch lifetime = new Stopwatch();
        Thread reader;
        volatile bool disposed;
        public string ReadError { get; private set; }
        public uint ChildPid { get { return child.ProcessId; } }
        public double ObservedSeconds { get { return lifetime.Elapsed.TotalSeconds; } }
        public bool Alive
        {
            get
            {
                uint state = WaitForSingleObject(child.hProcess, 0);
                if (state == 0) return false;
                if (state == 258) return true;
                throw new Win32Exception();
            }
        }
        public string Output { get { lock (outputLock) return Encoding.UTF8.GetString(captured.ToArray()); } }

        [StructLayout(LayoutKind.Sequential)] struct Coord { public short X, Y; }
        [StructLayout(LayoutKind.Sequential)]
        struct Startup
        {
            public int Size; public IntPtr Reserved, Desktop, Title;
            public uint X, Y, XSize, YSize, XChars, YChars, Fill, Flags;
            public ushort Show, ReservedBytes; public IntPtr ReservedData, Input, Output, Error;
        }
        [StructLayout(LayoutKind.Sequential)] struct StartupEx { public Startup Startup; public IntPtr Attributes; }
        [StructLayout(LayoutKind.Sequential)] struct ProcessInfo { public IntPtr hProcess, hThread; public uint ProcessId, ThreadId; }
        [StructLayout(LayoutKind.Sequential)]
        struct BasicLimits
        {
            public long ProcessTime, JobTime; public uint Flags; public UIntPtr MinWorkingSet, MaxWorkingSet;
            public uint ActiveProcesses; public UIntPtr Affinity; public uint Priority, Scheduling;
        }
        [StructLayout(LayoutKind.Sequential)] struct IoCounters { public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes; }
        [StructLayout(LayoutKind.Sequential)]
        struct JobLimits
        {
            public BasicLimits Basic; public IoCounters Io;
            public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
        }
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool CreatePipe(out IntPtr read, out IntPtr write, IntPtr security, uint size);
        [DllImport("kernel32.dll")] static extern int CreatePseudoConsole(Coord size, IntPtr input, IntPtr output, uint flags, out IntPtr console);
        [DllImport("kernel32.dll")] static extern void ClosePseudoConsole(IntPtr console);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool InitializeProcThreadAttributeList(IntPtr list, int count, uint flags, ref IntPtr size);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool UpdateProcThreadAttribute(IntPtr list, uint flags, IntPtr attribute,
            IntPtr value, IntPtr size, IntPtr previous, IntPtr returned);
        [DllImport("kernel32.dll")] static extern void DeleteProcThreadAttributeList(IntPtr list);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool CreateProcessW(string application, StringBuilder command, IntPtr processSecurity, IntPtr threadSecurity,
            bool inherit, uint flags, IntPtr environment, string directory, ref StartupEx startup, out ProcessInfo process);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern IntPtr CreateJobObjectW(IntPtr attributes, string name);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool SetInformationJobObject(IntPtr job, int kind, ref JobLimits limits, int size);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
        [DllImport("kernel32.dll", SetLastError = true)] static extern uint ResumeThread(IntPtr thread);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool ReadFile(IntPtr file, byte[] buffer, uint size, out uint read, IntPtr overlapped);
        [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
        [DllImport("kernel32.dll", SetLastError = true)] static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool TerminateProcess(IntPtr process, uint code);

        public ConPtySession(string directory)
        {
            try
            {
                if (!CreatePipe(out inputRead, out inputWrite, IntPtr.Zero, 0) ||
                    !CreatePipe(out outputRead, out outputWrite, IntPtr.Zero, 0)) throw new Win32Exception();
                reader = new Thread(Drain) { IsBackground = true, Name = "conpty-output" };
                reader.Start();
                int result = CreatePseudoConsole(new Coord { X = 100, Y = 30 }, inputRead, outputWrite, 0, out console);
                if (result < 0) Marshal.ThrowExceptionForHR(result);
                IntPtr size = IntPtr.Zero;
                InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref size);
                if (size == IntPtr.Zero || Marshal.GetLastWin32Error() != 122) throw new Win32Exception();
                attributes = Marshal.AllocHGlobal(size);
                if (!InitializeProcThreadAttributeList(attributes, 1, 0, ref size)) throw new Win32Exception();
                attributesReady = true;
                if (!UpdateProcThreadAttribute(attributes, 0, (IntPtr)PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                    console, (IntPtr)IntPtr.Size, IntPtr.Zero, IntPtr.Zero)) throw new Win32Exception();
                job = CreateJobObjectW(IntPtr.Zero, null);
                if (job == IntPtr.Zero) throw new Win32Exception();
                JobLimits limits = new JobLimits { Basic = new BasicLimits {
                    Flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | 8, ActiveProcesses = 8 } };
                if (!SetInformationJobObject(job, 9, ref limits, Marshal.SizeOf(typeof(JobLimits)))) throw new Win32Exception();
                StartupEx startup = new StartupEx { Attributes = attributes,
                    Startup = new Startup { Size = Marshal.SizeOf(typeof(StartupEx)) } };
                string cmd = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "cmd.exe");
                string ping = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "PING.EXE");
                StringBuilder command = new StringBuilder("\"" + cmd + "\" /d /s /c \"echo RYNHULT_CONPTY_OK & \"" + ping + "\" -t 127.0.0.1\"");
                if (!CreateProcessW(cmd, command, IntPtr.Zero, IntPtr.Zero, false,
                    0x80000 | CREATE_SUSPENDED, IntPtr.Zero, directory, ref startup, out child)) throw new Win32Exception();
                if (!AssignProcessToJobObject(job, child.hProcess)) throw new Win32Exception();
                if (ResumeThread(child.hThread) == UInt32.MaxValue) throw new Win32Exception();
                lifetime.Start();
                Close(ref inputRead); Close(ref outputWrite);
            }
            catch { Dispose(); throw; }
        }
        void Drain()
        {
            byte[] buffer = new byte[4096];
            while (true)
            {
                uint count;
                if (!ReadFile(outputRead, buffer, (uint)buffer.Length, out count, IntPtr.Zero))
                {
                    int code = Marshal.GetLastWin32Error();
                    if (!disposed) ReadError = new Win32Exception(code).Message;
                    return;
                }
                if (count == 0)
                {
                    if (!disposed) ReadError = "ConPTY output ended unexpectedly";
                    return;
                }
                lock (outputLock)
                {
                    int keep = (int)Math.Min(count, 65536 - captured.Length);
                    if (keep > 0) captured.Write(buffer, 0, keep);
                }
            }
        }
        static void Close(ref IntPtr handle)
        {
            if (handle != IntPtr.Zero) { CloseHandle(handle); handle = IntPtr.Zero; }
        }
        public void Dispose()
        {
            if (disposed) return;
            disposed = true;
            try
            {
                if (child.hProcess != IntPtr.Zero && Alive && !TerminateProcess(child.hProcess, 0))
                    throw new Win32Exception();
            }
            finally
            {
                Close(ref job); // Kill ping and descendants, even if cmd exits first or termination failed.
                Close(ref inputRead); Close(ref outputWrite); Close(ref inputWrite);
                if (console != IntPtr.Zero) { ClosePseudoConsole(console); console = IntPtr.Zero; }
                bool drained = reader == null || reader.Join(3000);
                Close(ref outputRead);
                Close(ref child.hThread); Close(ref child.hProcess);
                if (attributes != IntPtr.Zero)
                {
                    if (attributesReady) DeleteProcThreadAttributeList(attributes);
                    Marshal.FreeHGlobal(attributes); attributes = IntPtr.Zero;
                }
                if (drained) captured.Dispose();
                if (!drained) throw new System.TimeoutException("ConPTY output reader did not stop");
            }
        }
    }
}
