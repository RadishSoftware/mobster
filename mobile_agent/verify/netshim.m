// Mobster's network shim. Mobster launches an app in the iOS Simulator with this library in DYLD_INSERT_LIBRARIES and
// MOBSTER_PROXY_HOST / MOBSTER_PROXY_PORT set; every NSURLSession that sets no proxy of its own then goes through the
// run's local proxy. It reads nothing else, writes nothing, and exists only in that process.
#import <Foundation/Foundation.h>
#import <objc/runtime.h>

static NSDictionary *proxies(void) {
    const char *host = getenv("MOBSTER_PROXY_HOST"), *port = getenv("MOBSTER_PROXY_PORT");
    if (!host || !port) return nil;
    NSString *name = @(host);
    NSNumber *number = @(atoi(port));
    return @{@"HTTPEnable": @1, @"HTTPProxy": name, @"HTTPPort": number,
             @"HTTPSEnable": @1, @"HTTPSProxy": name, @"HTTPSPort": number};
}

static NSDictionary *(*original)(id, SEL);
static NSDictionary *replacement(id self, SEL command) {
    NSDictionary *own = original(self, command);
    return own ?: proxies();
}

__attribute__((constructor)) static void install(void) {
    if (!proxies()) return;
    Method method = class_getInstanceMethod([NSURLSessionConfiguration class], @selector(connectionProxyDictionary));
    if (!method) return;
    original = (NSDictionary *(*)(id, SEL))method_getImplementation(method);
    method_setImplementation(method, (IMP)replacement);
}
