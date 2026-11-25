// Loaded into the unchanged, already-authorized Meeting Audio Capture helper
// with DYLD_INSERT_LIBRARIES, only for `record`.
// 1) The helper sets CATapDescription.exclusive = NO after
//    initStereoGlobalTapButExcludeProcesses:@[]. That turns the empty
//    exclusion list into an empty *inclusion* list, so the tap captures no
//    process and delivers digital silence. Force exclusive = YES.
// 2) Opt the helper out of App Nap / timer throttling while recording.
#import <Foundation/Foundation.h>
#import <objc/runtime.h>

static void (*origSetExclusive)(id, SEL, BOOL);
static void fixedSetExclusive(id self, SEL _cmd, BOOL value) {
    (void)value;
    origSetExclusive(self, _cmd, YES);
}
static id activityToken;

__attribute__((constructor))
static void meetingNotesTapFix(void) {
    @autoreleasepool {
        NSArray *args = [[NSProcessInfo processInfo] arguments];
        if (args.count < 2 || ![args[1] isEqualToString:@"record"]) return;
        Class cls = objc_getClass("CATapDescription");
        if (cls) {
            Method m = class_getInstanceMethod(cls, sel_registerName("setExclusive:"));
            if (m) {
                origSetExclusive = (void (*)(id, SEL, BOOL))method_setImplementation(m, (IMP)fixedSetExclusive);
                fprintf(stderr, "capture: tapfix shim active (global tap stays exclusive)\n");
            }
        }
        activityToken = [[NSProcessInfo processInfo]
            beginActivityWithOptions:(NSActivityUserInitiated | NSActivityLatencyCritical | NSActivityIdleSystemSleepDisabled)
                              reason:@"Recording meeting audio"];
    }
}
