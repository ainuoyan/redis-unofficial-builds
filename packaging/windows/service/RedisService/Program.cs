using System.ComponentModel;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;

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

    internal sealed record Settings(
        string ConfigPath,
        string BindAddress,
        int Port,
        string? Password)
    {
        internal const int ShutdownTimeoutSeconds = 60;
    }

    private static int Main(string[] args)
    {
        if (args.Length == 1 && args[0] == "--self-test")
        {
            try
            {
                _ = RedisConfiguration.Load(PrefixPath());
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
            // A fatal wrapper or child-process failure must terminate without
            // reporting a clean SERVICE_STOPPED state so SCM recovery runs.
            Environment.Exit(1);
        }
    }

    private static void RunService()
    {
        RedisConfiguration config = RedisConfiguration.Load(PrefixPath());
        // Capture credentials once. Editing redis.conf while running must not
        // redirect shutdown to the next configuration's endpoint or password.
        Settings settings = new(config.ConfigPath, SelectEndpoint(config), config.Port, config.Password);
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
        process.OutputDataReceived += (_, eventArgs) => LogRedisOutput("stdout", eventArgs.Data, settings.Password);
        process.ErrorDataReceived += (_, eventArgs) => LogRedisOutput("stderr", eventArgs.Data, settings.Password);
        process.StartInfo.ArgumentList.Add(
            Path.GetRelativePath(prefix, configPath).Replace('\\', '/'));
        if (!process.Start())
        {
            throw new InvalidOperationException("Unable to start redis-server.exe.");
        }
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();

        try
        {
            if (!WaitForRedis(settings, process, TimeSpan.FromSeconds(60)))
            {
                if (process.HasExited)
                {
                    process.WaitForExit();
                    throw new InvalidOperationException(
                        $"redis-server.exe exited before readiness with code {process.ExitCode}.");
                }
                // Never send SHUTDOWN to an endpoint that did not pass readiness.
                throw new InvalidOperationException(
                    $"Redis did not pass readiness at {settings.BindAddress}:{settings.Port} within 60 seconds. Check bind, port, requirepass and the Redis startup log.");
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

            ReportStatus(ServiceStopPending, acceptedControls: 0, waitHint: Settings.ShutdownTimeoutSeconds * 1000);
            StopChild(process, settings);
        }
        finally
        {
            if (!process.HasExited)
            {
                Log("Terminating the managed Redis process after service failure.");
                process.Kill(entireProcessTree: true);
                process.WaitForExit(10_000);
            }
        }
    }

    internal static string SelectEndpoint(RedisConfiguration config)
    {
        foreach (RedisConfiguration.Binding binding in config.Bindings)
        {
            TcpListener? probe = null;
            try
            {
                probe = new(binding.Address, config.Port);
                probe.Server.ExclusiveAddressUse = true;
                // Fail before launching if another instance owns the endpoint.
                // A specific bind IP must be local, not an arbitrary remote host.
                probe.Start();
                return binding.Address.ToString();
            }
            catch (SocketException exception) when (binding.Optional &&
                exception.SocketErrorCode is SocketError.AddressNotAvailable or SocketError.AddressFamilyNotSupported)
            {
                continue;
            }
            catch (SocketException exception)
            {
                throw new InvalidOperationException(
                    $"Cannot use configured Redis endpoint {binding.Address}:{config.Port}: {exception.SocketErrorCode}. No Redis process was started.");
            }
            finally { probe?.Stop(); }
        }
        throw new InvalidOperationException("No configured Redis bind address is available on this machine.");
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
            // Do not inherit unrelated authentication from the wrapper environment.
            client.StartInfo.Environment.Remove("REDISCLI_AUTH");
            string? password = settings.Password;
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
            Task<string> stdout = client.StandardOutput.ReadToEndAsync();
            Task<string> stderr = client.StandardError.ReadToEndAsync();
            if (!client.WaitForExit(10_000))
            {
                client.Kill(entireProcessTree: true);
                client.WaitForExit(10_000);
                output = string.Empty;
                return false;
            }
            if (!Task.WaitAll([stdout, stderr], 10_000))
            {
                output = string.Empty;
                return false;
            }
            output = stdout.Result;
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

        if (process.WaitForExit(Settings.ShutdownTimeoutSeconds * 1000))
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

    private static void LogRedisOutput(string stream, string? line, string? password)
    {
        if (!string.IsNullOrEmpty(line))
        {
            Log($"redis {stream}: {(string.IsNullOrEmpty(password) ? line : line.Replace(password, "[redacted]", StringComparison.Ordinal))}");
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
