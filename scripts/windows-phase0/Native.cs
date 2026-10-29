// Safety sign-off: fixed local qualification APIs only; no command dispatcher or network listener.
// C# 5 / .NET Framework 4.x, shared by the PS 5.1 scripts and locally compiled service.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security;
using System.Security.Principal;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace Rynhult.Spike
{
    public static class Native
    {
        [StructLayout(LayoutKind.Sequential)]
        public struct SecurityAttributes { public int Length; public IntPtr Descriptor; public int Inherit; }
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern bool ConvertStringSecurityDescriptorToSecurityDescriptor(
            string sddl, uint revision, out IntPtr descriptor, out uint size);
        [DllImport("kernel32.dll")] public static extern IntPtr LocalFree(IntPtr memory);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool CreateDirectoryW(string path, ref SecurityAttributes attributes);

        public static SecurityAttributes Security(string sddl)
        {
            IntPtr descriptor; uint size;
            if (!ConvertStringSecurityDescriptorToSecurityDescriptor(sddl, 1, out descriptor, out size))
                throw new Win32Exception();
            return new SecurityAttributes { Length = Marshal.SizeOf(typeof(SecurityAttributes)), Descriptor = descriptor };
        }

        public static void CreateDirectory(string path, string sddl)
        {
            SecurityAttributes attributes = Security(sddl);
            try { if (!CreateDirectoryW(path, ref attributes)) throw new Win32Exception(); }
            finally { LocalFree(attributes.Descriptor); }
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern SafeFileHandle CreateFileW(string path, uint access, uint share,
            ref SecurityAttributes attributes, uint creation, uint flags, IntPtr template);
        public static void WriteNewFile(string path, byte[] bytes, string sddl)
        {
            SecurityAttributes attributes = Security(sddl);
            try
            {
                using (SafeFileHandle handle = CreateFileW(path, 0x40000000, 1, ref attributes, 1, 0x80, IntPtr.Zero))
                {
                    if (handle.IsInvalid) throw new Win32Exception();
                    using (FileStream stream = new FileStream(handle, FileAccess.Write))
                    {
                        stream.Write(bytes, 0, bytes.Length);
                        stream.Flush(true);
                    }
                }
            }
            finally { LocalFree(attributes.Descriptor); }
        }

        public static SecureString GenerateSecret()
        {
            byte[] bytes = new byte[64];
            SecureString result = new SecureString();
            using (System.Security.Cryptography.RandomNumberGenerator rng =
                   System.Security.Cryptography.RandomNumberGenerator.Create())
            {
                rng.GetBytes(bytes);
                // Fixed complexity characters carry no entropy; the random suffix supplies 384 bits.
                foreach (char c in "aA1!") result.AppendChar(c);
                const string alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
                foreach (byte b in bytes) result.AppendChar(alphabet[b & 63]);
            }
            Array.Clear(bytes, 0, bytes.Length);
            result.MakeReadOnly();
            return result;
        }

        public static byte[] ProtectSecret(SecureString value)
        {
            byte[] bytes = new byte[value.Length * 2];
            IntPtr clear = IntPtr.Zero;
            try
            {
                clear = Marshal.SecureStringToGlobalAllocUnicode(value);
                Marshal.Copy(clear, bytes, 0, bytes.Length);
                return System.Security.Cryptography.ProtectedData.Protect(bytes, null,
                    System.Security.Cryptography.DataProtectionScope.LocalMachine);
            }
            finally
            {
                Array.Clear(bytes, 0, bytes.Length);
                if (clear != IntPtr.Zero) Marshal.ZeroFreeGlobalAllocUnicode(clear);
            }
        }

        [StructLayout(LayoutKind.Sequential)]
        struct LsaAttributes
        {
            public uint Length; public IntPtr RootDirectory; public IntPtr ObjectName;
            public uint Attributes; public IntPtr SecurityDescriptor; public IntPtr SecurityQualityOfService;
        }
        [StructLayout(LayoutKind.Sequential)]
        struct LsaString { public ushort Length; public ushort MaximumLength; public IntPtr Buffer; }
        [DllImport("advapi32.dll")]
        static extern uint LsaOpenPolicy(IntPtr system, ref LsaAttributes attributes, uint access, out IntPtr policy);
        [DllImport("advapi32.dll")] static extern uint LsaClose(IntPtr policy);
        [DllImport("advapi32.dll")] static extern uint LsaNtStatusToWinError(uint status);
        [DllImport("advapi32.dll")] static extern uint LsaFreeMemory(IntPtr memory);
        [DllImport("advapi32.dll")]
        static extern uint LsaEnumerateAccountRights(IntPtr policy, byte[] sid, out IntPtr rights, out uint count);
        [DllImport("advapi32.dll")]
        static extern uint LsaAddAccountRights(IntPtr policy, byte[] sid, LsaString[] rights, uint count);
        [DllImport("advapi32.dll")]
        static extern uint LsaRemoveAccountRights(IntPtr policy, byte[] sid,
            [MarshalAs(UnmanagedType.U1)] bool all, IntPtr rights, uint count);

        static byte[] Sid(string sid)
        {
            SecurityIdentifier value = new SecurityIdentifier(sid);
            byte[] result = new byte[value.BinaryLength];
            value.GetBinaryForm(result, 0);
            return result;
        }
        static void LsaCheck(uint status)
        {
            if (status != 0) throw new Win32Exception((int)LsaNtStatusToWinError(status));
        }
        static IntPtr Policy()
        {
            LsaAttributes attributes = new LsaAttributes { Length = (uint)Marshal.SizeOf(typeof(LsaAttributes)) };
            IntPtr policy;
            LsaCheck(LsaOpenPolicy(IntPtr.Zero, ref attributes, 0x810, out policy));
            return policy;
        }
        public static string[] GetRights(string sid)
        {
            IntPtr policy = Policy(), buffer = IntPtr.Zero; uint count;
            try
            {
                uint status = LsaEnumerateAccountRights(policy, Sid(sid), out buffer, out count);
                if (status == 0xC0000034) return new string[0]; // No LSA account/assignments.
                LsaCheck(status);
                string[] result = new string[count];
                for (int i = 0; i < count; i++)
                {
                    LsaString right = (LsaString)Marshal.PtrToStructure(
                        IntPtr.Add(buffer, i * Marshal.SizeOf(typeof(LsaString))), typeof(LsaString));
                    result[i] = Marshal.PtrToStringUni(right.Buffer, right.Length / 2);
                }
                return result;
            }
            finally { if (buffer != IntPtr.Zero) LsaFreeMemory(buffer); LsaClose(policy); }
        }
        public static void AddRights(string sid, string[] names)
        {
            IntPtr policy = Policy();
            LsaString[] rights = new LsaString[names.Length];
            try
            {
                for (int i = 0; i < names.Length; i++)
                    rights[i] = new LsaString { Buffer = Marshal.StringToHGlobalUni(names[i]),
                        Length = checked((ushort)(names[i].Length * 2)),
                        MaximumLength = checked((ushort)((names[i].Length + 1) * 2)) };
                LsaCheck(LsaAddAccountRights(policy, Sid(sid), rights, (uint)rights.Length));
            }
            finally
            {
                foreach (LsaString right in rights) if (right.Buffer != IntPtr.Zero) Marshal.FreeHGlobal(right.Buffer);
                LsaClose(policy);
            }
        }
        public static void RemoveRights(string sid)
        {
            IntPtr policy = Policy();
            try
            {
                uint status = LsaRemoveAccountRights(policy, Sid(sid), true, IntPtr.Zero, 0);
                if (status != 0xC0000034) LsaCheck(status);
            }
            finally { LsaClose(policy); }
        }

        [DllImport("userenv.dll", CharSet = CharSet.Unicode)]
        static extern int CreateProfile(string sid, string user, StringBuilder path, uint length);
        [DllImport("userenv.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool DeleteProfileW(string sid, string path, string computer);
        public static string CreateProfile(string sid, string user)
        {
            StringBuilder path = new StringBuilder(260);
            int result = CreateProfile(sid, user, path, (uint)path.Capacity);
            if (result != 0) throw new COMException("CreateProfile did not create a new profile", result);
            return path.ToString();
        }
        public static void DeleteProfile(string sid)
        {
            if (!DeleteProfileW(sid, null, null)) throw new Win32Exception();
        }

        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern IntPtr OpenSCManagerW(string machine, string database, uint access);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern IntPtr CreateServiceW(IntPtr manager, string name, string displayName, uint access,
            uint type, uint start, uint error, string binary, string group, IntPtr tag,
            string dependencies, string account, IntPtr secret);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern IntPtr OpenServiceW(IntPtr manager, string name, uint access);
        [DllImport("advapi32.dll", SetLastError = true)] static extern bool DeleteService(IntPtr service);
        [DllImport("advapi32.dll")] static extern bool CloseServiceHandle(IntPtr handle);
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        struct Description { [MarshalAs(UnmanagedType.LPWStr)] public string Text; }
        [DllImport("advapi32.dll", EntryPoint = "ChangeServiceConfig2W", SetLastError = true)]
        static extern bool SetDescription(IntPtr service, uint level, ref Description value);
        [DllImport("advapi32.dll", EntryPoint = "ChangeServiceConfig2W", SetLastError = true)]
        static extern bool SetDelayedStart(IntPtr service, uint level, ref int value);
        [DllImport("advapi32.dll", SetLastError = true)]
        static extern bool SetServiceObjectSecurity(IntPtr service, uint information, IntPtr descriptor);
        [StructLayout(LayoutKind.Sequential)]
        struct ServiceStatus
        {
            public uint Type, State, Controls, Error, SpecificError, Checkpoint, WaitHint, ProcessId, Flags;
        }
        [DllImport("advapi32.dll", SetLastError = true)]
        static extern bool QueryServiceStatusEx(IntPtr service, int level, out ServiceStatus status, int size, out int needed);

        public static void CreateService(string name, string binary, string user, SecureString value,
                                         string marker, string ownerSid)
        {
            IntPtr manager = OpenSCManagerW(null, null, 2);
            if (manager == IntPtr.Zero) throw new Win32Exception();
            IntPtr clear = IntPtr.Zero, service = IntPtr.Zero;
            try
            {
                clear = Marshal.SecureStringToGlobalAllocUnicode(value);
                service = CreateServiceW(manager, name, name, 0xF01FF, 0x10, 2, 1, "\"" + binary + "\"",
                    null, IntPtr.Zero, null, ".\\" + user, clear);
                if (service == IntPtr.Zero) throw new Win32Exception();
                Description description = new Description { Text = marker };
                if (!SetDescription(service, 1, ref description)) throw new Win32Exception();
                int delayed = 1;
                if (!SetDelayedStart(service, 3, ref delayed)) throw new Win32Exception();
                SecurityAttributes security = Security("D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;0x00020005;;;" + ownerSid + ")");
                try { if (!SetServiceObjectSecurity(service, 4, security.Descriptor)) throw new Win32Exception(); }
                finally { LocalFree(security.Descriptor); }
            }
            finally
            {
                if (clear != IntPtr.Zero) Marshal.ZeroFreeGlobalAllocUnicode(clear);
                if (service != IntPtr.Zero) CloseServiceHandle(service);
                CloseServiceHandle(manager);
            }
        }
        public static uint ServiceProcessId(string name)
        {
            IntPtr manager = OpenSCManagerW(null, null, 1);
            if (manager == IntPtr.Zero) throw new Win32Exception();
            IntPtr service = IntPtr.Zero;
            try
            {
                service = OpenServiceW(manager, name, 4);
                if (service == IntPtr.Zero) throw new Win32Exception();
                ServiceStatus status; int needed;
                if (!QueryServiceStatusEx(service, 0, out status, Marshal.SizeOf(typeof(ServiceStatus)), out needed))
                    throw new Win32Exception();
                if (status.State != 4 || status.ProcessId == 0) throw new InvalidOperationException("Probe service is not running");
                return status.ProcessId;
            }
            finally { if (service != IntPtr.Zero) CloseServiceHandle(service); CloseServiceHandle(manager); }
        }
        public static void RemoveService(string name)
        {
            IntPtr manager = OpenSCManagerW(null, null, 1);
            if (manager == IntPtr.Zero) throw new Win32Exception();
            IntPtr service = IntPtr.Zero;
            try
            {
                service = OpenServiceW(manager, name, 0x10000);
                if (service == IntPtr.Zero) throw new Win32Exception();
                if (!DeleteService(service)) throw new Win32Exception();
            }
            finally { if (service != IntPtr.Zero) CloseServiceHandle(service); CloseServiceHandle(manager); }
        }

        // SERVICE_CONFIG_REQUIRED_PRIVILEGES_INFO (6): SCM removes every privilege not listed from the service's token
        // when it starts the process. The list is a multi-string: names separated by NUL, ending in two NULs.
        [StructLayout(LayoutKind.Sequential)]
        struct RequiredPrivilegesInfo { public IntPtr Names; }
        [DllImport("advapi32.dll", EntryPoint = "ChangeServiceConfig2W", SetLastError = true)]
        static extern bool SetRequiredPrivileges(IntPtr service, uint level, ref RequiredPrivilegesInfo value);
        [DllImport("advapi32.dll", EntryPoint = "QueryServiceConfig2W", SetLastError = true)]
        static extern bool QueryServiceConfig(IntPtr service, uint level, IntPtr buffer, int size, out int needed);
        public static void RequireOnlyPrivileges(string name, string[] privileges)
        {
            if (privileges == null || privileges.Length == 0) throw new ArgumentException("Name at least one privilege");
            foreach (string privilege in privileges)
                if (String.IsNullOrEmpty(privilege) || privilege.IndexOf('\0') >= 0) throw new ArgumentException("Bad privilege name");
            IntPtr manager = OpenSCManagerW(null, null, 1);
            if (manager == IntPtr.Zero) throw new Win32Exception();
            IntPtr service = IntPtr.Zero, names = IntPtr.Zero;
            try
            {
                service = OpenServiceW(manager, name, 2); // SERVICE_CHANGE_CONFIG
                if (service == IntPtr.Zero) throw new Win32Exception();
                names = Marshal.StringToHGlobalUni(String.Join("\0", privileges) + "\0\0");
                RequiredPrivilegesInfo info = new RequiredPrivilegesInfo { Names = names };
                if (!SetRequiredPrivileges(service, 6, ref info)) throw new Win32Exception();
            }
            finally
            {
                if (names != IntPtr.Zero) Marshal.FreeHGlobal(names);
                if (service != IntPtr.Zero) CloseServiceHandle(service);
                CloseServiceHandle(manager);
            }
        }
        public static string[] RequiredPrivileges(string name)
        {
            IntPtr manager = OpenSCManagerW(null, null, 1);
            if (manager == IntPtr.Zero) throw new Win32Exception();
            IntPtr service = IntPtr.Zero, buffer = IntPtr.Zero;
            try
            {
                service = OpenServiceW(manager, name, 1); // SERVICE_QUERY_CONFIG: the owner has it (CreateService's DACL)
                if (service == IntPtr.Zero) throw new Win32Exception();
                int needed;
                QueryServiceConfig(service, 6, IntPtr.Zero, 0, out needed);
                if (Marshal.GetLastWin32Error() != 122 || needed <= 0 || needed > 65536) throw new Win32Exception();
                buffer = Marshal.AllocHGlobal(needed);
                if (!QueryServiceConfig(service, 6, buffer, needed, out needed)) throw new Win32Exception();
                List<string> result = new List<string>();
                IntPtr cursor = Marshal.ReadIntPtr(buffer); // NULL: no required privileges configured
                while (cursor != IntPtr.Zero && result.Count < 64)
                {
                    string privilege = Marshal.PtrToStringUni(cursor);
                    if (String.IsNullOrEmpty(privilege)) break;
                    result.Add(privilege);
                    cursor = IntPtr.Add(cursor, (privilege.Length + 1) * 2);
                }
                return result.ToArray();
            }
            finally
            {
                if (buffer != IntPtr.Zero) Marshal.FreeHGlobal(buffer);
                if (service != IntPtr.Zero) CloseServiceHandle(service);
                CloseServiceHandle(manager);
            }
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool GetNamedPipeServerProcessId(SafePipeHandle pipe, out uint pid);
        public static int Read(PipeStream stream, byte[] buffer, int offset, int count)
        {
            IAsyncResult pending = stream.BeginRead(buffer, offset, count, null, null);
            using (System.Threading.WaitHandle wait = pending.AsyncWaitHandle)
            {
                if (!wait.WaitOne(3000)) throw new TimeoutException("Pipe read exceeded three seconds");
                return stream.EndRead(pending);
            }
        }
        public static string QueryPipe(string name, uint expectedPid)
        {
            using (NamedPipeClientStream pipe = new NamedPipeClientStream(".", name, PipeDirection.InOut,
                       PipeOptions.Asynchronous, TokenImpersonationLevel.Identification))
            {
                pipe.Connect(3000);
                uint pid;
                if (!GetNamedPipeServerProcessId(pipe.SafePipeHandle, out pid)) throw new Win32Exception();
                if (pid != expectedPid) throw new InvalidOperationException("Pipe server PID differs from SCM");
                pipe.WriteByte(0x50);
                pipe.Flush();
                byte[] buffer = new byte[4096];
                int length = 0;
                while (length < buffer.Length)
                {
                    int count = Read(pipe, buffer, length, buffer.Length - length);
                    if (count == 0) throw new EndOfStreamException("Pipe closed without a response");
                    length += count;
                    int newline = Array.IndexOf(buffer, (byte)10, 0, length);
                    if (newline >= 0)
                    {
                        pipe.WriteByte(0x41); // Bounded server-side ACK wait avoids an unbounded pipe drain.
                        pipe.Flush();
                        return Encoding.UTF8.GetString(buffer, 0, newline);
                    }
                }
                throw new InvalidDataException("Pipe response exceeds 4096 bytes");
            }
        }
    }
}
