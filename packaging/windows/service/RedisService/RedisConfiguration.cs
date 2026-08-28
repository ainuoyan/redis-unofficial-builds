using System.Globalization;
using System.Net;
using System.Text;

namespace RedisUnofficial.Service;

// Only interpret options needed for service control. Redis validates other options.
// Never include directive values in errors: they may contain credentials.
internal sealed class RedisConfiguration
{
    internal sealed record Binding(IPAddress Address, bool Optional);

    private static readonly UTF8Encoding Utf8 = new(false, true);
    private readonly string _prefix;
    private readonly HashSet<string> _activeFiles = new(StringComparer.OrdinalIgnoreCase);
    private string _workingDirectory;
    private int _filesRead;
    private long _bytesRead;
    private bool _daemonize;
    private string _supervised = "no";
    private string _aclFile = "";
    private int _tlsPort;

    internal string ConfigPath { get; }
    internal int Port { get; private set; } = 6379;
    internal string? Password { get; private set; }
    internal IReadOnlyList<Binding> Bindings { get; private set; } =
        [new(IPAddress.Loopback, false), new(IPAddress.IPv6Loopback, true)];

    private RedisConfiguration(string prefix)
    {
        _prefix = Path.GetFullPath(prefix);
        _workingDirectory = _prefix;
        ConfigPath = Path.Combine(_prefix, "conf", "redis.conf");
    }

    internal static RedisConfiguration Load(string prefix)
    {
        RedisConfiguration config = new(prefix);
        config.ReadFile(config.ConfigPath, 0);
        if (config.Port == 0 || config._tlsPort != 0)
            throw new InvalidOperationException("The Windows service requires a plain TCP port; TLS-only and socket-only configurations are not supported.");
        if (config._daemonize || config._supervised != "no")
            throw new InvalidOperationException("The Windows service requires daemonize no and supervised no in redis.conf.");
        if (config._aclFile.Length != 0)
            throw new InvalidOperationException("aclfile is not supported by automatic service authentication. Use requirepass; ACL password hashes cannot be recovered from redis.conf.");
        return config;
    }

    private void ReadFile(string path, int depth)
    {
        if (depth >= 16 || ++_filesRead > 64)
            throw new InvalidOperationException("Redis configuration include limit exceeded.");
        path = ResolvePath(path);
        RequireManagedFile(path);
        if (!_activeFiles.Add(path))
            throw new InvalidOperationException("Redis configuration contains an include cycle.");
        try
        {
            using FileStream stream = File.OpenRead(path);
            _bytesRead += stream.Length;
            if (_bytesRead > 4 * 1024 * 1024)
                throw new InvalidOperationException("Redis configuration exceeds the 4 MiB service parsing limit.");
            byte[] bytes = new byte[checked((int)stream.Length)];
            stream.ReadExactly(bytes);
            if (stream.ReadByte() != -1)
                throw new InvalidOperationException("Redis configuration changed while being read; retry with stable files.");
            string contents;
            try { contents = Utf8.GetString(bytes); }
            catch (DecoderFallbackException)
            {
                throw new InvalidOperationException("Redis configuration must be valid UTF-8.");
            }
            if (contents.Contains('\0') || contents.StartsWith('\uFEFF'))
                throw new InvalidOperationException("Redis configuration must not contain NUL bytes or a UTF-8 BOM.");
            int lineNumber = 0;
            foreach (string line in contents.Split('\n'))
            {
                lineNumber++;
                try
                {
                    if (line.Length > 65536)
                        throw new InvalidOperationException("Configuration line exceeds the service parsing limit.");
                    string trimmed = line.Trim(' ', '\t', '\r', '\n');
                    if (trimmed.Length == 0 || trimmed.StartsWith('#')) continue;
                    Apply(Tokenize(trimmed), depth);
                }
                catch (InvalidOperationException exception)
                {
                    throw new InvalidOperationException($"{Path.GetFileName(path)}:{lineNumber}: {exception.Message}");
                }
            }
        }
        finally { _activeFiles.Remove(path); }
    }

    private void Apply(List<string> args, int depth)
    {
        if (args.Count == 0) return;
        string directive = args[0].ToLowerInvariant();
        switch (directive)
        {
            case "include":
                RequireCount(args, 2);
                if (args[1].IndexOfAny(['*', '?', '[', ']']) >= 0)
                    throw new InvalidOperationException("Include globs are not supported by the Windows service; use explicit include paths.");
                ReadFile(args[1], depth + 1);
                break;
            case "dir":
                RequireCount(args, 2);
                // Redis resolves includes against its current working directory,
                // not the including file's directory. dir changes it immediately.
                string directory = ResolvePath(args[1]);
                if (!Directory.Exists(directory))
                    throw new InvalidOperationException("The configured dir does not exist.");
                _workingDirectory = directory;
                break;
            case "bind":
                if (args.Count < 2 || args.Count > 17)
                    throw new InvalidOperationException("bind requires 1 to 16 numeric IP addresses.");
                List<Binding> bindings = [];
                foreach (string item in args.Skip(1))
                {
                    bool optional = item.StartsWith('-');
                    if (!IPAddress.TryParse(optional ? item[1..] : item, out IPAddress? address))
                        throw new InvalidOperationException("bind must contain numeric IPv4 or IPv6 addresses, optionally prefixed with '-'.");
                    if (address.Equals(IPAddress.Any)) address = IPAddress.Loopback;
                    if (address.Equals(IPAddress.IPv6Any)) address = IPAddress.IPv6Loopback;
                    bindings.Add(new(address, optional));
                }
                Bindings = bindings.OrderBy(binding => IPAddress.IsLoopback(binding.Address) ? 0 : 1).ToArray();
                break;
            case "port":
                Port = ReadPort(args);
                break;
            case "tls-port":
                _tlsPort = ReadPort(args);
                break;
            case "requirepass":
                RequireCount(args, 2);
                if (Utf8.GetByteCount(args[1]) > 4096)
                    throw new InvalidOperationException("requirepass exceeds the service authentication limit.");
                Password = args[1].Length == 0 ? null : args[1];
                break;
            case "aclfile":
                RequireCount(args, 2);
                _aclFile = args[1];
                break;
            case "user":
                throw new InvalidOperationException("Inline ACL users are not supported by automatic service authentication. Use requirepass; do not remove ACL rules without reviewing access permissions.");
            case "daemonize":
                RequireCount(args, 2);
                if (!args[1].Equals("yes", StringComparison.OrdinalIgnoreCase) &&
                    !args[1].Equals("no", StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("daemonize requires yes or no.");
                _daemonize = args[1].Equals("yes", StringComparison.OrdinalIgnoreCase);
                break;
            case "supervised":
                RequireCount(args, 2);
                _supervised = args[1].ToLowerInvariant();
                break;
            case "rename-command":
                RequireCount(args, 3);
                if (new[] { "ping", "shutdown", "auth" }.Contains(args[1], StringComparer.OrdinalIgnoreCase))
                    throw new InvalidOperationException("The Windows service requires unrenamed PING, AUTH and SHUTDOWN commands.");
                break;
            case "sentinel":
                throw new InvalidOperationException("Sentinel configuration is not supported by this Redis service.");
        }
    }

    private string ResolvePath(string candidate)
    {
        if (candidate.Length == 0)
            throw new InvalidOperationException("Empty configuration paths are not supported.");
        // Redis uses the MSYS2 /c/... convention for absolute Windows paths.
        if (OperatingSystem.IsWindows() && candidate.Length >= 3 && candidate[0] == '/' &&
            char.IsAsciiLetter(candidate[1]) && candidate[2] == '/')
            candidate = candidate[1] + ":" + candidate[2..];
        if (OperatingSystem.IsWindows() && !Path.IsPathFullyQualified(candidate) &&
            (Path.IsPathRooted(candidate) || candidate.Contains(':')))
            throw new InvalidOperationException("Ambiguous Windows configuration path; use a relative or fully qualified drive path.");
        return Path.GetFullPath(candidate, _workingDirectory);
    }

    private void RequireManagedFile(string path)
    {
        string boundary = _prefix.TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        StringComparison comparison = OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal;
        if (!path.StartsWith(boundary, comparison))
            throw new InvalidOperationException("Configuration includes must remain inside the Redis installation directory.");
        for (string? item = path; item is not null; item = Path.GetDirectoryName(item))
        {
            if ((File.GetAttributes(item) & FileAttributes.ReparsePoint) != 0)
                throw new InvalidOperationException("Configuration paths must not contain reparse points or symbolic links.");
            if (string.Equals(item, _prefix, comparison)) break;
        }
    }

    private static void RequireCount(List<string> args, int count)
    {
        if (args.Count != count)
            throw new InvalidOperationException("Invalid argument count; Redis does not support trailing inline comments.");
    }

    private static int ReadPort(List<string> args)
    {
        RequireCount(args, 2);
        if (!int.TryParse(args[1], NumberStyles.None, CultureInfo.InvariantCulture, out int port) || port > 65535)
            throw new InvalidOperationException("Invalid TCP port in redis.conf.");
        return port;
    }

    internal static List<string> Tokenize(string line)
    {
        // Match Redis sdssplitargs byte semantics, including quoted hex escapes.
        // This is not shell parsing: unquoted backslashes and '#' are literal.
        byte[] input = Utf8.GetBytes(line);
        List<string> result = [];
        int index = 0;
        while (index < input.Length)
        {
            while (index < input.Length && IsSpace(input[index])) index++;
            if (index == input.Length) break;
            List<byte> token = [];
            byte quote = 0;
            while (true)
            {
                if (index == input.Length)
                {
                    if (quote != 0) throw new InvalidOperationException("Unterminated quote in redis.conf.");
                    break;
                }
                byte value = input[index++];
                if (quote == 0)
                {
                    if (value is (byte)' ' or (byte)'\t' or (byte)'\r' or (byte)'\n') break;
                    if (value is (byte)'"' or (byte)'\'') quote = value;
                    else token.Add(value);
                }
                else if (value == quote)
                {
                    if (index < input.Length && !IsSpace(input[index]))
                        throw new InvalidOperationException("A closing quote must be followed by whitespace.");
                    break;
                }
                else if (value == '\\' && index < input.Length && quote == '"')
                {
                    byte escaped = input[index++];
                    if (escaped == 'x' && index + 1 < input.Length &&
                        Hex(input[index]) >= 0 && Hex(input[index + 1]) >= 0)
                    {
                        token.Add((byte)(Hex(input[index]) * 16 + Hex(input[index + 1])));
                        index += 2;
                    }
                    else token.Add(escaped switch
                    {
                        (byte)'n' => (byte)'\n', (byte)'r' => (byte)'\r', (byte)'t' => (byte)'\t',
                        (byte)'b' => (byte)'\b', (byte)'a' => (byte)'\a', _ => escaped
                    });
                }
                else if (value == '\\' && quote == '\'' && index < input.Length && input[index] == '\'')
                {
                    token.Add(input[index++]);
                }
                else token.Add(value);
            }
            if (token.Contains(0)) throw new InvalidOperationException("NUL escapes are not supported by the Windows service.");
            try { result.Add(Utf8.GetString(token.ToArray())); }
            catch (DecoderFallbackException)
            {
                throw new InvalidOperationException("Configuration arguments must be valid UTF-8.");
            }
        }
        return result;
    }

    private static bool IsSpace(byte value) => value is 32 or 9 or 10 or 11 or 12 or 13;
    private static int Hex(byte value) => value switch
    {
        >= (byte)'0' and <= (byte)'9' => value - '0',
        >= (byte)'a' and <= (byte)'f' => value - 'a' + 10,
        >= (byte)'A' and <= (byte)'F' => value - 'A' + 10,
        _ => -1
    };
}
