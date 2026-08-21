using System.ComponentModel;
using System.Diagnostics;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text.Json;

namespace RedisUnofficial.Service;

internal static class Program
{
    private const string ServiceName = "RedisUnofficial";
    private const uint ServiceWin32OwnProcess = 0x10;
    private const uint ServiceStartPending = 0x2;
    private const uint ServiceStopPending = 0x3;
    private const uint ServiceRunning = 0x4;
    private const uint ServiceStopped = 0x1;
    private const uint ServiceAcceptStop = 0x1;
    private const uint ServiceAcceptShutdown = 0x4;
    private const uint ServiceAcceptPreshutdown = 0x100;
    private const uint ControlStop = 0x1;
    private const uint ControlShutdown = 0x5;
    private const uint ControlPreshutdown = 0xF;
    private const int FailedServiceControllerConnect = 1063;

    private static readonly ManualResetEventSlim StopRequested = new(false);
    private static readonly ServiceMainDelegate ServiceMainCallback = ServiceMain;
    private static readonly HandlerExDelegate HandlerCallback = Handler;
    private static IntPtr _statusHandle;
    private static uint _checkpoint;

    private sealed record Settings(string ConfigPath, string BindAddress, int Port, int ShutdownTimeoutSeconds);

    private static int Main(string[] args)
    {
        if (args.Length == 1 && args[0] == "--self-test")
        {
            try
            {
                Settings settings = LoadSettings();
                ResolveManagedPath(settings.ConfigPath, "configuration");
                Console.WriteLine("RedisService self-test passed.");
                return 0;
            }
            catch (Exception exception)
            {
                Console.Error.WriteLine($"RedisService self-test failed: {exception.Message}");
                return 2;
            }
        }

        if (args.Length != 1 || args[0] != "--service")
        {
            Console.Error.WriteLine("Usage: RedisService.exe --service | --self-test");
            return 2;
        }

        ServiceTableEntry[] table =
        [
            new ServiceTableEntry { ServiceName = ServiceName, ServiceMain = ServiceMainCallback },
            new ServiceTableEntry()
        ];
        if (!StartServiceCtrlDispatcher(table))
        {
            int error = Marshal.GetLastWin32Error();
            if (error == FailedServiceControllerConnect)
            {
                Console.Error.WriteLine("RedisService --service must be launched by Windows SCM.");
                return 2;
            }
            throw new Win32Exception(error, "StartServiceCtrlDispatcher failed");
        }
        return 0;
    }

    private static void ServiceMain(int argumentCount, IntPtr arguments)
    {
        _statusHandle = RegisterServiceCtrlHandlerEx(ServiceName, HandlerCallback, IntPtr.Zero);
        if (_statusHandle == IntPtr.Zero)
        {
            return;
        }
        ReportStatus(ServiceStartPending, acceptedControls: 0, waitHint: 60_000);
        try
        {
            RunService();
            ReportStatus(ServiceStopped, acceptedControls: 0, win32ExitCode: 0);
        }
        catch (Exception exception)
        {
            Log($"Service failure: {exception}");
            ReportStatus(ServiceStopped, acceptedControls: 0, win32ExitCode: 1066, serviceExitCode: 1);
        }
    }

    private static void RunService()
    {
        Settings settings = LoadSettings();
        string configPath = ResolveManagedPath(settings.ConfigPath, "configuration");
        string prefix = PrefixPath();
        string serverPath = ResolveManagedPath(Path.Combine(prefix, "bin", "redis-server.exe"), "server");
        if (!File.Exists(serverPath) || !File.Exists(configPath))
        {
            throw new InvalidOperationException("Redis server or configuration is missing.");
        }

        using Process process = new()
        {
            StartInfo = new ProcessStartInfo(serverPath)
            {
                WorkingDirectory = prefix,
                UseShellExecute = false,
                CreateNoWindow = true,
            },
            EnableRaisingEvents = true,
        };
        process.StartInfo.ArgumentList.Add(ToMsysPath(configPath));
        if (!process.Start())
        {
            throw new InvalidOperationException("Unable to start redis-server.exe.");
        }

        if (!WaitForRedis(settings, process, TimeSpan.FromSeconds(60)))
        {
            StopChild(process, settings);
            throw new InvalidOperationException("Redis did not pass readiness within 60 seconds.");
        }
        ReportStatus(
            ServiceRunning,
            ServiceAcceptStop | ServiceAcceptShutdown | ServiceAcceptPreshutdown);

        while (!StopRequested.Wait(500))
        {
            if (process.HasExited)
            {
                throw new InvalidOperationException($"redis-server.exe exited with code {process.ExitCode}.");
            }
        }

        ReportStatus(ServiceStopPending, acceptedControls: 0, waitHint: (uint)(settings.ShutdownTimeoutSeconds * 1000));
        StopChild(process, settings);
    }

    private static Settings LoadSettings()
    {
        string path = Path.Combine(PrefixPath(), "RedisService.json");
        if (!File.Exists(path))
        {
            throw new InvalidOperationException("RedisService.json is missing.");
        }
        Settings? settings = JsonSerializer.Deserialize<Settings>(
            File.ReadAllText(path),
            new JsonSerializerOptions { PropertyNameCaseInsensitive = false });
        if (settings is null
            || settings.BindAddress != "127.0.0.1"
            || settings.Port is < 1 or > 65535
            || settings.ShutdownTimeoutSeconds is < 5 or > 300)
        {
            throw new InvalidOperationException("RedisService.json violates the managed service contract.");
        }
        return settings;
    }

    private static bool WaitForRedis(Settings settings, Process process, TimeSpan timeout)
    {
        Stopwatch timer = Stopwatch.StartNew();
        while (timer.Elapsed < timeout && !process.HasExited && !StopRequested.IsSet)
        {
            try
            {
                using TcpClient client = new();
                Task connect = client.ConnectAsync(settings.BindAddress, settings.Port);
                if (connect.Wait(TimeSpan.FromSeconds(1)) && client.Connected)
                {
                    using NetworkStream stream = client.GetStream();
                    stream.Write("*1\r\n$4\r\nPING\r\n"u8);
                    stream.ReadTimeout = 1000;
                    byte[] response = new byte[16];
                    int count = stream.Read(response, 0, response.Length);
                    if (count >= 7 && response.AsSpan(0, 7).SequenceEqual("+PONG\r\n"u8))
                    {
                        return true;
                    }
                }
            }
            catch (Exception exception) when (exception is SocketException or IOException or AggregateException)
            {
                // Redis is still starting.
            }
            Thread.Sleep(500);
        }
        return false;
    }

    private static void StopChild(Process process, Settings settings)
    {
        if (process.HasExited)
        {
            return;
        }
        string clientPath = ResolveManagedPath(Path.Combine(PrefixPath(), "bin", "redis-cli.exe"), "client");
        try
        {
            using Process client = new()
            {
                StartInfo = new ProcessStartInfo(clientPath)
                {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                }
            };
            client.StartInfo.ArgumentList.Add("-h");
            client.StartInfo.ArgumentList.Add(settings.BindAddress);
            client.StartInfo.ArgumentList.Add("-p");
            client.StartInfo.ArgumentList.Add(settings.Port.ToString(System.Globalization.CultureInfo.InvariantCulture));
            client.StartInfo.ArgumentList.Add("shutdown");
            client.Start();
            client.WaitForExit(10_000);
        }
        catch (Exception exception)
        {
            Log($"Graceful shutdown command failed: {exception.Message}");
        }

        if (process.WaitForExit(settings.ShutdownTimeoutSeconds * 1000))
        {
            return;
        }
        Log("Redis did not stop within the configured timeout; terminating its process tree.");
        process.Kill(entireProcessTree: true);
        process.WaitForExit(10_000);
    }

    private static uint Handler(uint control, uint eventType, IntPtr eventData, IntPtr context)
    {
        if (control is ControlStop or ControlShutdown or ControlPreshutdown)
        {
            StopRequested.Set();
            ReportStatus(ServiceStopPending, acceptedControls: 0, waitHint: 300_000);
        }
        return 0;
    }

    private static string PrefixPath() => Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, ".."));

    private static string ResolveManagedPath(string candidate, string description)
    {
        string prefix = PrefixPath().TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        string path = Path.GetFullPath(candidate);
        if (!path.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) || path.Contains('\0'))
        {
            throw new InvalidOperationException($"The {description} path escapes the managed prefix.");
        }
        return path;
    }

    private static string ToMsysPath(string path)
    {
        string full = Path.GetFullPath(path);
        if (full.Length < 3 || !char.IsAsciiLetter(full[0]) || full[1] != ':' || full[2] != '\\')
        {
            throw new InvalidOperationException("Only local drive paths are supported by the MSYS2 backend.");
        }
        return $"/{char.ToLowerInvariant(full[0])}/{full[3..].Replace('\\', '/')}";
    }

    private static void Log(string message)
    {
        try
        {
            string logDirectory = Path.Combine(PrefixPath(), "log");
            Directory.CreateDirectory(logDirectory);
            File.AppendAllText(
                Path.Combine(logDirectory, "service-wrapper.log"),
                $"{DateTimeOffset.UtcNow:O} {message}{Environment.NewLine}");
        }
        catch
        {
            // Logging must never hide the original service failure.
        }
    }

    private static void ReportStatus(
        uint state,
        uint acceptedControls,
        uint win32ExitCode = 0,
        uint serviceExitCode = 0,
        uint waitHint = 0)
    {
        ServiceStatus status = new()
        {
            ServiceType = ServiceWin32OwnProcess,
            CurrentState = state,
            ControlsAccepted = acceptedControls,
            Win32ExitCode = win32ExitCode,
            ServiceSpecificExitCode = serviceExitCode,
            CheckPoint = state is ServiceStartPending or ServiceStopPending ? ++_checkpoint : 0,
            WaitHint = waitHint,
        };
        if (_statusHandle != IntPtr.Zero && !SetServiceStatus(_statusHandle, ref status))
        {
            Log($"SetServiceStatus failed with Win32 error {Marshal.GetLastWin32Error()}.");
        }
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct ServiceTableEntry
    {
        [MarshalAs(UnmanagedType.LPWStr)] public string? ServiceName;
        public ServiceMainDelegate? ServiceMain;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ServiceStatus
    {
        public uint ServiceType;
        public uint CurrentState;
        public uint ControlsAccepted;
        public uint Win32ExitCode;
        public uint ServiceSpecificExitCode;
        public uint CheckPoint;
        public uint WaitHint;
    }

    [UnmanagedFunctionPointer(CallingConvention.Winapi)]
    private delegate void ServiceMainDelegate(int argumentCount, IntPtr arguments);

    [UnmanagedFunctionPointer(CallingConvention.Winapi)]
    private delegate uint HandlerExDelegate(uint control, uint eventType, IntPtr eventData, IntPtr context);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool StartServiceCtrlDispatcher([In] ServiceTableEntry[] serviceTable);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr RegisterServiceCtrlHandlerEx(
        string serviceName,
        HandlerExDelegate handler,
        IntPtr context);

    [DllImport("advapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetServiceStatus(IntPtr statusHandle, ref ServiceStatus serviceStatus);
}
