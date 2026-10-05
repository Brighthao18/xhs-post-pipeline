# Native Chrome compatibility

This local module retains `github.com/xpzouying/headless_browser v0.4.0` source
and license. The root module uses a local `replace`; the global module cache is
unchanged.

`WithNativeChrome` disables CloakBrowser-specific command-line flags, custom UA
and Client-Hints overrides, and JavaScript stealth injection. It removes the
upstream forced `--no-sandbox` (and `disable-setuid-sandbox`) for native Chrome.
Ordinary launcher automation indicators remain intact. Other upstream modes keep
their existing behavior. No network, proxy, seed, or ordinary browser session is
altered or imported.

Offline verification: `go test github.com/xpzouying/headless_browser -run
'^TestNativeChrome' -count=1` from the root module. Tests build configuration and
launch arguments; they never launch a browser or call a platform endpoint.
