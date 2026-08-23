using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
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
    private static readonly object LogLock = new();
    private static readonly ServiceMainDelegate ServiceMainCallback = ServiceMain;
    private static readonly HandlerExDelegate HandlerCallback = Handler;
    private static IntPtr _statusHandle;
    private static uint _checkpoint;

    private sealed record Settings(
        string ConfigPath,
        string BindAddress,
        int Port,
        int ShutdownTimeoutSeconds,
        string? Username = null,
        string? PasswordFile = null);

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
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            },
            EnableRaisingEvents = true,
        };
        process.OutputDataReceived += (_, eventArgs) => LogRedisOutput("stdout", eventArgs.Data);
        process.ErrorDataReceived += (_, eventArgs) => LogRedisOutput("stderr", eventArgs.Data);
        process.StartInfo.ArgumentList.Add(
            Path.GetRelativePath(prefix, configPath).Replace('\\', '/'));
        if (!process.Start())
        {
            throw new InvalidOperationException("Unable to start redis-server.exe.");
        }
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();

        if (!WaitForRedis(settings, process, TimeSpan.FromSeconds(60)))
        {
            if (process.HasExited)
            {
                process.WaitForExit();
                throw new InvalidOperationException(
                    $"redis-server.exe exited before readiness with code {process.ExitCode}.");
            }
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
            || settings.ShutdownTimeoutSeconds is < 5 or > 300
            || (settings.Username is not null
                && (settings.PasswordFile is null
                    || settings.Username.Length is < 1 or > 256
                    || settings.Username.IndexOfAny(['\0', '\r', '\n']) >= 0)))
        {
            throw new InvalidOperationException("RedisService.json violates the managed service contract.");
        }
        _ = LoadPassword(settings);
        return settings;
    }

    private static bool WaitForRedis(Settings settings, Process process, TimeSpan timeout)
    {
        Stopwatch timer = Stopwatch.StartNew();
        while (timer.Elapsed < timeout && !process.HasExited && !StopRequested.IsSet)
        {
            if (RunRedisCli(settings, "ping", out string output)
                && output.Trim().Equals("PONG", StringComparison.Ordinal))
            {
                Thread.Sleep(100);
                return !process.HasExited;
            }
            Thread.Sleep(500);
        }
        return false;
    }

    private static string? LoadPassword(Settings settings)
    {
        if (settings.PasswordFile is null)
        {
            return null;
        }
        string passwordPath = ResolveManagedPath(settings.PasswordFile, "password file");
        FileInfo passwordFile = new(passwordPath);
        if (!passwordFile.Exists || passwordFile.Length is < 1 or > 4096)
        {
            throw new InvalidOperationException("The managed Redis password file is missing or invalid.");
        }
        string password = File.ReadAllText(passwordPath, new UTF8Encoding(false, true));
        if (password.Length is < 1 or > 1024 || password.IndexOfAny(['\0', '\r', '\n']) >= 0)
        {
            throw new InvalidOperationException("The managed Redis password file has invalid content.");
        }
        return password;
    }

    private static bool RunRedisCli(Settings settings, string command, out string output)
    {
        string clientPath = ResolveManagedPath(Path.Combine(PrefixPath(), "bin", "redis-cli.exe"), "client");
        try
        {
            using Process client = new()
            {
                StartInfo = new ProcessStartInfo(clientPath)
                {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                }
            };
            client.StartInfo.ArgumentList.Add("-h");
            client.StartInfo.ArgumentList.Add(settings.BindAddress);
            client.StartInfo.ArgumentList.Add("-p");
            client.StartInfo.ArgumentList.Add(settings.Port.ToString(System.Globalization.CultureInfo.InvariantCulture));
            if (settings.Username is not null)
            {
                client.StartInfo.ArgumentList.Add("--user");
                client.StartInfo.ArgumentList.Add(settings.Username);
            }
            string? password = LoadPassword(settings);
            if (password is not null)
            {
                client.StartInfo.Environment["REDISCLI_AUTH"] = password;
            }
            client.StartInfo.ArgumentList.Add(command);
            if (!client.Start())
            {
                output = string.Empty;
                return false;
            }
            string stdout = client.StandardOutput.ReadToEnd();
            _ = client.StandardError.ReadToEnd();
            if (!client.WaitForExit(10_000))
            {
                client.Kill(entireProcessTree: true);
                client.WaitForExit(10_000);
                output = string.Empty;
                return false;
            }
            output = stdout;
            return client.ExitCode == 0;
        }
        catch (Exception exception) when (exception is IOException or InvalidOperationException or System.Security.SecurityException)
        {
            Log($"Redis client command failed: {exception.Message}");
            output = string.Empty;
            return false;
        }
    }

    private static void StopChild(Process process, Settings settings)
    {
        if (process.HasExited)
        {
            return;
        }
        if (!RunRedisCli(settings, "shutdown", out _))
        {
            Log("Graceful shutdown command failed.");
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
        string path = Path.GetFullPath(
            Path.IsPathFullyQualified(candidate) ? candidate : Path.Combine(PrefixPath(), candidate));
        if (!path.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) || path.Contains('\0'))
        {
            throw new InvalidOperationException($"The {description} path escapes the managed prefix.");
        }
        return path;
    }

    private static void Log(string message)
    {
        try
        {
            lock (LogLock)
            {
                string logDirectory = Path.Combine(PrefixPath(), "log");
                Directory.CreateDirectory(logDirectory);
                File.AppendAllText(
                    Path.Combine(logDirectory, "service-wrapper.log"),
                    $"{DateTimeOffset.UtcNow:O} {message}{Environment.NewLine}");
            }
        }
        catch
        {
            // Logging must never hide the original service failure.
        }
    }

    private static void LogRedisOutput(string stream, string? line)
    {
        if (!string.IsNullOrEmpty(line))
        {
            Log($"redis {stream}: {line}");
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
