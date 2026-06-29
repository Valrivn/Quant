import logging
import random
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

CDP_EVASION_SCRIPT = """
// === CDP STEALTH EVASION ===
// Override navigator.webdriver
Object.defineProperty(Object.getPrototypeOf(navigator), 'webdriver', {
    get: () => undefined,
    configurable: true,
});

// Override navigator.plugins length
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const arr = [1, 2, 3, 4, 5];
        arr.__proto__ = PluginArray.prototype;
        arr.item = (i) => arr[i];
        arr.namedItem = () => null;
        arr.refresh = () => {};
        return arr;
    },
    configurable: true,
});

// Override navigator.languages
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en'],
    configurable: true,
});

// Override navigator.platform
Object.defineProperty(navigator, 'platform', {
    get: () => 'MacIntel',
    configurable: true,
});

// Override navigator.hardwareConcurrency
Object.defineProperty(navigator, 'hardwareConcurrency', {
    get: () => 8,
    configurable: true,
});

// Override navigator.deviceMemory
Object.defineProperty(navigator, 'deviceMemory', {
    get: () => 8,
    configurable: true,
});

// Override navigator.maxTouchPoints
Object.defineProperty(navigator, 'maxTouchPoints', {
    get: () => 0,
    configurable: true,
});

// Override navigator.connection
if (navigator.connection) {
    Object.defineProperty(navigator.connection, 'rtt', {
        get: () => 100,
        configurable: true,
    });
    Object.defineProperty(navigator.connection, 'downlink', {
        get: () => 10,
        configurable: true,
    });
    Object.defineProperty(navigator.connection, 'effectiveType', {
        get: () => '4g',
        configurable: true,
    });
}

// Override chrome object
if (!window.chrome) {
    window.chrome = {
        runtime: {},
        loadTimes: function() {},
        csi: function() {},
        app: {},
    };
}

// Override Permissions
if (navigator.permissions) {
    const origQuery = navigator.permissions.query;
    navigator.permissions.query = (params) => (
        params.name === 'notifications' ? Promise.resolve({ state: 'denied' }) : origQuery(params)
    );
}

// Override WebGL vendor/renderer
const getParameterProxyHandler = {
    apply: function(target, ctx, args) {
        const param = args[0];
        const webglVendor = 'Google Inc. (Intel)';
        const webglRenderer = 'ANGLE (Intel, Intel(R) UHD Graphics 630, OpenGL 4.1)';
        const unmaskedVendor = 'Intel';
        const unmaskedRenderer = 'Intel Iris OpenGL Engine';
        if (param === 37445) return webglVendor;
        if (param === 37446) return webglRenderer;
        if (param === 7936) return unmaskedVendor;
        if (param === 7937) return unmaskedRenderer;
        return target.apply(ctx, args);
    }
};
if (HTMLCanvasElement.prototype.getContext) {
    const origGetContext = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function(type, attrs) {
        const ctx = origGetContext.call(this, type, attrs);
        if (type === 'webgl' || type === 'experimental-webgl') {
            if (ctx.getParameter) {
                ctx.getParameter = new Proxy(ctx.getParameter, getParameterProxyHandler);
            }
        }
        return ctx;
    };
}

// Override canvas fingerprint noise
const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
HTMLCanvasElement.prototype.toDataURL = function(type) {
    const canvas = this;
    const context = canvas.getContext('2d');
    if (context) {
        const imageData = context.getImageData(0, 0, canvas.width, canvas.height);
        if (imageData && imageData.data) {
            imageData.data[0] = imageData.data[0] ^ 0;
        }
    }
    return origToDataURL.call(this, type);
};

// Ensure navigator.userAgent is not headless
Object.defineProperty(navigator, 'userAgent', {
    get: () => '%USER_AGENT%',
    configurable: true,
});

// Override screen properties
Object.defineProperty(screen, 'width', { get: () => %VIEWPORT_WIDTH%, configurable: true });
Object.defineProperty(screen, 'height', { get: () => %VIEWPORT_HEIGHT%, configurable: true });
Object.defineProperty(screen, 'availWidth', { get: () => %VIEWPORT_WIDTH%, configurable: true });
Object.defineProperty(screen, 'availHeight', { get: () => %VIEWPORT_HEIGHT%, configurable: true });
Object.defineProperty(screen, 'colorDepth', { get: () => 24, configurable: true });
Object.defineProperty(screen, 'pixelDepth', { get: () => 24, configurable: true });

// Override navigator.mediaDevices (no fake mic/camera)
if (navigator.mediaDevices) {
    navigator.mediaDevices.enumerateDevices = () => Promise.resolve([
        { deviceId: '', groupId: '', kind: 'audioinput', label: '', toJSON: () => ({}) },
        { deviceId: '', groupId: '', kind: 'audiooutput', label: '', toJSON: () => ({}) },
        { deviceId: '', groupId: '', kind: 'videoinput', label: '', toJSON: () => ({}) },
    ]);
}

// Override Battery API
if (navigator.getBattery) {
    navigator.getBattery = () => Promise.resolve({
        charging: true,
        chargingTime: 0,
        dischargingTime: Infinity,
        level: 1.0,
        onchargingchange: null,
        onchargingtimechange: null,
        ondischargingtimechange: null,
        onlevelchange: null,
    });
}
"""

VIEWPORTS = [
    (1920, 1080),
    (1440, 900),
    (1366, 768),
    (1536, 864),
    (1280, 800),
    (1920, 1200),
    (1680, 1050),
    (1600, 900),
    (1280, 720),
    (1440, 1080),
]

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]

CLOUDFLARE_SIGNALS = [
    "cf-browser-verification", "cf-challenge", "just a moment",
    "checking your browser", "attention required", "security check",
    "access to this page has been denied", "confirm you are a human",
    "press & hold", "verify you are human", "checking if the site connection is secure",
    "we've detected unusual traffic", "cloudflare ray id",
    "turnstile", "cf-turnstile", "challenge-form",
]


def build_cdp_evasion_script(viewport: tuple, user_agent: str) -> str:
    w, h = viewport
    script = CDP_EVASION_SCRIPT
    script = script.replace("%VIEWPORT_WIDTH%", str(w))
    script = script.replace("%VIEWPORT_HEIGHT%", str(h))
    script = script.replace("%USER_AGENT%", user_agent)
    return script


def build_cdp_cmds(viewport: tuple) -> List[Dict]:
    w, h = viewport
    return [
        {"cmd": "Emulation.setDeviceMetricsOverride", "params": {
            "width": w, "height": h, "mobile": False, "deviceScaleFactor": 1,
        }},
        {"cmd": "Emulation.setTouchEmulationEnabled", "params": {"enabled": False}},
        {"cmd": "Network.setUserAgentOverride", "params": {
            "userAgent": random.choice(USER_AGENTS),
            "acceptLanguage": "en-US,en;q=0.9",
            "platform": "MacIntel",
        }},
        {"cmd": "Emulation.setLocale", "params": {"locale": "en-US"}},
        {"cmd": "Emulation.setTimezoneOverride", "params": {"timezoneId": "America/New_York"}},
        {"cmd": "Emulation.setGeolocationOverride", "params": {
            "latitude": 40.7128, "longitude": -74.0060, "accuracy": 10,
        }},
    ]


def random_viewport() -> tuple:
    return random.choice(VIEWPORTS)


def random_user_agent() -> str:
    return random.choice(USER_AGENTS)


def detect_cloudflare(html: str) -> bool:
    if not html:
        return False
    html_lower = html.lower()
    return any(signal in html_lower for signal in CLOUDFLARE_SIGNALS)
