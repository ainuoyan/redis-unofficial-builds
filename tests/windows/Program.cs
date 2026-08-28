using System.Globalization;
using System.Net;
using System.Net.Sockets;
using System.Text;
using RedisUnofficial.Service;
using ServiceProgram = RedisUnofficial.Service.Program;

internal static class WindowsServiceTests
{
    private static int Main()
    {
        (string Name, Action Test)[] tests =
        [
            ("defaults without JSON", () => WithConfig("", root =>
            {
                RedisConfiguration config = RedisConfiguration.Load(root);
                Equal(6379, config.Port);
                Equal<string?>(null, config.Password);
                Equal(IPAddress.Loopback, config.Bindings[0].Address);
            })),
            ("nondefault IP port and password", () => WithConfig(
                "bind 192.168.1.10\nport 16379\nrequirepass 'space # secret'\n", root =>
                {
                    RedisConfiguration config = RedisConfiguration.Load(root);
                    Equal(16379, config.Port);
                    Equal("192.168.1.10", config.Bindings[0].Address.ToString());
                    Equal("space # secret", config.Password);
                })),
            ("last directive wins", () => WithConfig(
                "port 6379\nPoRt 16379\nrequirepass old\nrequirepass new\nbind 0.0.0.0\nbind ::1\n", root =>
                {
                    RedisConfiguration config = RedisConfiguration.Load(root);
                    Equal(16379, config.Port);
                    Equal("new", config.Password);
                    Equal(IPAddress.IPv6Loopback, config.Bindings[0].Address);
                })),
            ("quoted hex and UTF8", () => WithConfig("requirepass \"x\\x23\\xe5\\xaf\\x86\\xe7\\xa0\\x81\\t\\\"y\"\n", root =>
                Equal("x#密码\t\"y", RedisConfiguration.Load(root).Password))),
            ("single quotes and unquoted backslash", () =>
            {
                Equal("it's\\literal", RedisConfiguration.Tokenize("'it\\'s\\literal'")[0]);
                Equal("abc\\def#xyz", RedisConfiguration.Tokenize("abc\\def#xyz")[0]);
                Equal("prefix space", RedisConfiguration.Tokenize("prefix\" space\"")[0]);
            }),
            ("empty password clears auth", () => WithConfig("requirepass old\nrequirepass \"\"\n", root =>
                Equal<string?>(null, RedisConfiguration.Load(root).Password))),
            ("masterauth is not client auth", () => WithConfig("masterauth replication-secret\n", root =>
                Equal<string?>(null, RedisConfiguration.Load(root).Password))),
            ("wildcard bind uses loopback", () => WithConfig("bind 0.0.0.0 -::\n", root =>
            {
                RedisConfiguration config = RedisConfiguration.Load(root);
                Equal(IPAddress.Loopback, config.Bindings[0].Address);
                Equal(IPAddress.IPv6Loopback, config.Bindings[1].Address);
                Equal(true, config.Bindings[1].Optional);
            })),
            ("prefer explicitly bound loopback", () => WithConfig("bind 192.168.1.10 127.0.0.1\n", root =>
                Equal(IPAddress.Loopback, RedisConfiguration.Load(root).Bindings[0].Address))),
            ("include uses Redis cwd and ordering", () => WithConfig(
                "dir data\ninclude ../conf/site.conf\nport 16380\n", root =>
                {
                    Write(root, "conf/site.conf", "port 16379\nrequirepass included\ninclude ../conf/next.conf\n");
                    Write(root, "conf/next.conf", "bind 192.168.1.10\n");
                    RedisConfiguration config = RedisConfiguration.Load(root);
                    Equal(16380, config.Port);
                    Equal("included", config.Password);
                    Equal("192.168.1.10", config.Bindings[0].Address.ToString());
                })),
            ("include can repeat without a cycle", () => WithConfig(
                "include conf/site.conf\nport 16380\ninclude conf/site.conf\n", root =>
                {
                    Write(root, "conf/site.conf", "port 16379\n");
                    Equal(16379, RedisConfiguration.Load(root).Port);
                })),
            ("include dir changes propagate to parent", () => WithConfig(
                "include conf/site.conf\ninclude ../conf/next.conf\n", root =>
                {
                    Write(root, "conf/site.conf", "dir data\n");
                    Write(root, "conf/next.conf", "port 16379\n");
                    Equal(16379, RedisConfiguration.Load(root).Port);
                })),
            ("include cycle", () => WithConfig("include conf/redis.conf\n", root => Reject(root, "cycle"))),
            ("include depth limit", () => WithConfig("include conf/0.conf\n", root =>
            {
                for (int i = 0; i < 17; i++) Write(root, $"conf/{i}.conf", $"include conf/{i + 1}.conf\n");
                Reject(root, "limit");
            })),
            ("include file count limit", () => WithConfig(string.Concat(Enumerable.Repeat("include conf/empty.conf\n", 65)), root =>
            {
                Write(root, "conf/empty.conf", "");
                Reject(root, "limit");
            })),
            ("include traversal rejected", () => WithConfig("include ../outside.conf\n", root => Reject(root, "inside"))),
            ("include globs rejected explicitly", () => WithConfig("include conf/*.conf\n", root => Reject(root, "globs"))),
            ("include symlink rejected", () => WithConfig("include conf/link.conf\n", root =>
            {
                if (OperatingSystem.IsWindows()) return; // Unix CI exercises the same reparse-point guard.
                Write(root, "conf/real.conf", "port 16379\n");
                File.CreateSymbolicLink(Path.Combine(root, "conf/link.conf"), "real.conf");
                Reject(root, "reparse");
            })),
            ("missing include fails closed", () => WithConfig("include conf/missing.conf\n", root =>
                Throws<IOException>(() => RedisConfiguration.Load(root)))),
            ("port zero rejected", () => WithConfig("port 0\n", root => Reject(root, "plain TCP"))),
            ("invalid ports", () =>
            {
                foreach (string port in new[] { "-1", "65536", "1e3", "999999999999999999", "abc" })
                    WithConfig($"port {port}\n", root => Reject(root, "port"));
            }),
            ("no trailing inline comments", () => WithConfig("port 16379 # comment\n", root => Reject(root, "argument count"))),
            ("no unsupported hostname binds", () => WithConfig("bind not-a-local-IP\n", root => Reject(root, "numeric"))),
            ("ACL file fails clearly", () => WithConfig("aclfile conf/users.acl\n", root => Reject(root, "aclfile"))),
            ("inline ACL fails without disclosing password", () => WithConfig("user app on >dont-print-this +@all\n", root =>
                Reject(root, "ACL", "dont-print-this"))),
            ("daemonization rejected", () => WithConfig("daemonize yes\n", root => Reject(root, "daemonize no"))),
            ("daemonization final value wins", () => WithConfig("daemonize yes\ndaemonize no\n", root => RedisConfiguration.Load(root))),
            ("supervision rejected", () => WithConfig("supervised systemd\n", root => Reject(root, "supervised no"))),
            ("TLS rejected", () => WithConfig("tls-port 6380\n", root => Reject(root, "TLS"))),
            ("renamed control commands rejected", () =>
            {
                foreach (string command in new[] { "PING", "AUTH", "SHUTDOWN" })
                    WithConfig($"rename-command {command} hidden\n", root => Reject(root, "unrenamed"));
            }),
            ("sentinel rejected", () => WithConfig("sentinel monitor primary 127.0.0.1 6379 1\n", root => Reject(root, "Sentinel"))),
            ("unbalanced quote hides secret", () => WithConfig("requirepass \"dont-print-this\n", root =>
                Reject(root, "quote", "dont-print-this"))),
            ("quote suffix rejected", () => WithConfig("requirepass \"dont-print-quote-value\"suffix\n", root =>
                Reject(root, "closing quote", "dont-print-quote-value"))),
            ("NUL escape rejected", () => WithConfig("requirepass \"a\\x00b\"\n", root => Reject(root, "NUL"))),
            ("invalid escaped UTF8 rejected", () => WithConfig("requirepass \"\\xff\"\n", root => Reject(root, "UTF-8"))),
            ("BOM rejected", () => WithConfig("\uFEFFport 16379\n", root => Reject(root, "BOM"))),
            ("oversized line rejected", () => WithConfig(new string('x', 65537), root => Reject(root, "line exceeds"))),
            ("oversized config rejected", () => WithConfig(new string('x', 4 * 1024 * 1024 + 1), root => Reject(root, "4 MiB"))),
            ("legacy JSON ignored", () => WithConfig("port 16379\nrequirepass current\n", root =>
            {
                Write(root, "RedisService.json", "not even JSON");
                Equal("current", RedisConfiguration.Load(root).Password);
            })),
            ("stop settings retain startup credentials", () => WithConfig("port 16379\nrequirepass old\n", root =>
            {
                RedisConfiguration config = RedisConfiguration.Load(root);
                ServiceProgram.Settings settings = new(config.ConfigPath, "127.0.0.1", config.Port, config.Password);
                Write(root, "conf/redis.conf", "port 26379\nrequirepass new\n");
                Equal(16379, settings.Port);
                Equal("old", settings.Password);
                Equal(26379, RedisConfiguration.Load(root).Port);
            })),
            ("occupied endpoint fails before Redis starts", () =>
            {
                TcpListener listener = new(IPAddress.Loopback, 0);
                listener.Server.ExclusiveAddressUse = true;
                listener.Start();
                try
                {
                    int port = ((IPEndPoint)listener.LocalEndpoint).Port;
                    WithConfig($"bind 127.0.0.1\nport {port}\n", root =>
                        Throws<InvalidOperationException>(() => ServiceProgram.SelectEndpoint(RedisConfiguration.Load(root))));
                }
                finally { listener.Stop(); }
            }),
            ("optional IPv6 loopback or IPv4 fallback", () =>
            {
                TcpListener listener = new(IPAddress.Loopback, 0);
                listener.Start();
                int port = ((IPEndPoint)listener.LocalEndpoint).Port;
                listener.Stop();
                WithConfig($"bind -::1 127.0.0.1\nport {port}\n", root =>
                {
                    string address = ServiceProgram.SelectEndpoint(RedisConfiguration.Load(root));
                    Equal(true, address is "::1" or "127.0.0.1");
                });
            }),
            ("culture-independent parsing", () =>
            {
                CultureInfo previous = CultureInfo.CurrentCulture;
                try
                {
                    CultureInfo.CurrentCulture = CultureInfo.GetCultureInfo("tr-TR");
                    WithConfig("BIND 127.0.0.1\nPORT 16379\nREQUIREPASS secret\n", root =>
                        Equal(16379, RedisConfiguration.Load(root).Port));
                }
                finally { CultureInfo.CurrentCulture = previous; }
            })
        ];
        int failures = 0;
        foreach ((string name, Action test) in tests)
        {
            try { test(); Console.WriteLine($"PASS {name}"); }
            catch (Exception exception) { failures++; Console.Error.WriteLine($"FAIL {name}: {exception.Message}"); }
        }
        Console.WriteLine($"Windows service tests: {tests.Length - failures} passed, {failures} failed.");
        return failures == 0 ? 0 : 1;
    }

    private static void WithConfig(string config, Action<string> test)
    {
        string root = Directory.CreateTempSubdirectory("redis-wrapper-test-空 格-").FullName;
        try
        {
            Directory.CreateDirectory(Path.Combine(root, "conf"));
            Directory.CreateDirectory(Path.Combine(root, "data"));
            Write(root, "conf/redis.conf", config);
            test(root);
        }
        finally { Directory.Delete(root, recursive: true); }
    }

    private static void Write(string root, string name, string contents) =>
        File.WriteAllText(Path.Combine(root, name), contents, new UTF8Encoding(false));

    private static void Equal<T>(T expected, T actual)
    {
        if (!EqualityComparer<T>.Default.Equals(expected, actual)) throw new Exception("Values differ.");
    }

    private static void Throws<T>(Action test) where T : Exception
    {
        try { test(); }
        catch (T) { return; }
        throw new Exception($"Expected {typeof(T).Name}.");
    }

    private static void Reject(string root, string message, string? secret = null)
    {
        try { RedisConfiguration.Load(root); }
        catch (InvalidOperationException exception)
        {
            if (!exception.Message.Contains(message, StringComparison.OrdinalIgnoreCase)) throw;
            if (secret is not null && exception.ToString().Contains(secret, StringComparison.Ordinal))
                throw new Exception("Error disclosed a credential.");
            return;
        }
        throw new Exception("Invalid configuration was accepted.");
    }
}
