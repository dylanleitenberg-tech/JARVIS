/* JARVIS.app's main executable.
 *
 * It has to be a real Mach-O, not a shell script: a code-signed bundle whose
 * CFBundleExecutable is a script cannot validate, and LaunchServices refuses
 * to launch it (-10669). It also has to exist so that macOS has a stable,
 * signable identity to attach the Accessibility grant to — the venv's Python
 * cannot hold one itself.
 *
 * All it does is find the project root relative to itself, then exec the
 * assistant so the bundle stays the responsible process.
 */
#include <libgen.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <signal.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <sys/wait.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <ApplicationServices/ApplicationServices.h>

/* ---------------------------------------------------------------- control
 *
 * Accessibility is NOT attributed through the responsible process the way the
 * camera and microphone are: AXIsProcessTrusted checks the code signature of
 * the process that calls it. The Python child therefore can never be trusted,
 * however the app is ticked — only this binary, which is the app, can be.
 *
 * So this process posts the events. Python sends one line per action over a
 * unix socket and this performs it.
 */

/* The forked assistant, so it can be taken down with this process. */
static volatile pid_t g_child = -1;

/* Ask the child to stop and wait for it to let the camera go, then insist.
 * Signal-handler safe: only kill(), waitpid() and _exit(). */
static void reap_child(void) {
    pid_t child = g_child;
    if (child <= 0) return;
    g_child = -1;
    kill(child, SIGTERM);
    /* Roughly three seconds, in tenths, so a normal shutdown is not cut
     * short and a wedged one does not hold the device indefinitely. */
    for (int i = 0; i < 30; i++) {
        int status = 0;
        pid_t done = waitpid(child, &status, WNOHANG);
        if (done == child || (done < 0 && errno != EINTR)) return;
        usleep(100000);
    }
    kill(child, SIGKILL);
    while (waitpid(child, NULL, 0) < 0 && errno == EINTR) { }
}

static void stop_child(int sig) {
    reap_child();
    _exit(sig == SIGINT ? 130 : 0);
}

/* The assistant exited on its own — powered down from the HUD, or crashed.
 * There is nothing left to relay events to, so this process goes too rather
 * than lingering as a resident app that does nothing. */
static void child_gone(int sig) {
    (void)sig;
    pid_t child = g_child;
    if (child <= 0) return;
    if (waitpid(child, NULL, WNOHANG) == child) {
        g_child = -1;
        _exit(0);
    }
}

static CGEventFlags parse_flags(unsigned long bits) {
    CGEventFlags f = 0;
    if (bits & 1) f |= kCGEventFlagMaskCommand;
    if (bits & 2) f |= kCGEventFlagMaskShift;
    if (bits & 4) f |= kCGEventFlagMaskAlternate;
    if (bits & 8) f |= kCGEventFlagMaskControl;
    return f;
}

static void post_mouse(CGEventType type, double x, double y,
                       CGMouseButton button, CGEventFlags flags, int clicks) {
    CGEventRef e = CGEventCreateMouseEvent(NULL, type, CGPointMake(x, y), button);
    if (!e) return;
    if (flags) CGEventSetFlags(e, flags);
    if (clicks > 1) CGEventSetIntegerValueField(e, kCGMouseEventClickState, clicks);
    CGEventPost(kCGHIDEventTap, e);
    CFRelease(e);
}

static void handle(const char *line) {
    char verb[32] = {0};
    double x = 0, y = 0;
    unsigned long button = 0, bits = 0, extra = 0;
    int n = sscanf(line, "%31s %lf %lf %lu %lu %lu",
                   verb, &x, &y, &button, &bits, &extra);
    if (n < 1) return;
    CGEventFlags flags = parse_flags(bits);
    CGMouseButton btn = (CGMouseButton)button;

    static const CGEventType down[] = {kCGEventLeftMouseDown, kCGEventRightMouseDown,
                                       kCGEventOtherMouseDown};
    static const CGEventType up[]   = {kCGEventLeftMouseUp, kCGEventRightMouseUp,
                                       kCGEventOtherMouseUp};
    static const CGEventType drag[] = {kCGEventLeftMouseDragged, kCGEventRightMouseDragged,
                                       kCGEventOtherMouseDragged};
    if (button > 2) button = 0;

    if (!strcmp(verb, "move")) {
        post_mouse(kCGEventMouseMoved, x, y, kCGMouseButtonLeft, 0, 1);
    } else if (!strcmp(verb, "down")) {
        CGPoint p = CGEventGetLocation(CGEventCreate(NULL));
        post_mouse(down[button], p.x, p.y, btn, flags, 1);
    } else if (!strcmp(verb, "up")) {
        CGPoint p = CGEventGetLocation(CGEventCreate(NULL));
        post_mouse(up[button], p.x, p.y, btn, flags, 1);
    } else if (!strcmp(verb, "drag")) {
        post_mouse(drag[button], x, y, btn, flags, 1);
    } else if (!strcmp(verb, "click")) {
        CGPoint p = CGEventGetLocation(CGEventCreate(NULL));
        int clicks = extra ? (int)extra : 1;
        for (int i = 1; i <= clicks; i++) {
            post_mouse(down[button], p.x, p.y, btn, flags, i);
            post_mouse(up[button], p.x, p.y, btn, flags, i);
        }
    } else if (!strcmp(verb, "scroll")) {
        CGEventRef e = CGEventCreateScrollWheelEvent(NULL, kCGScrollEventUnitPixel,
                                                     2, (int32_t)x, (int32_t)y);
        if (e) { CGEventPost(kCGHIDEventTap, e); CFRelease(e); }
    } else if (!strcmp(verb, "key")) {
        CGEventRef kd = CGEventCreateKeyboardEvent(NULL, (CGKeyCode)x, true);
        CGEventRef ku = CGEventCreateKeyboardEvent(NULL, (CGKeyCode)x, false);
        if (kd && ku) {
            CGEventSetFlags(kd, parse_flags((unsigned long)y));
            CGEventSetFlags(ku, parse_flags((unsigned long)y));
            CGEventPost(kCGHIDEventTap, kd);
            CGEventPost(kCGHIDEventTap, ku);
        }
        if (kd) CFRelease(kd);
        if (ku) CFRelease(ku);
    }
}

static void *serve_thread(void *arg);

static void control_server(const char *sock_path) {
    unlink(sock_path);
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) return;
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, sock_path, sizeof(addr.sun_path) - 1);
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) return;
    if (listen(fd, 4) < 0) return;

    for (;;) {
        int c = accept(fd, NULL, NULL);
        if (c < 0) { if (errno == EINTR) continue; break; }
        char buf[512];
        ssize_t got;
        while ((got = read(c, buf, sizeof(buf) - 1)) > 0) {
            buf[got] = 0;
            char *save = NULL;
            for (char *l = strtok_r(buf, "\n", &save); l; l = strtok_r(NULL, "\n", &save))
                handle(l);
            const char ok = '.';
            if (write(c, &ok, 1) != 1) break;
        }
        close(c);
    }
}


static void *serve_thread(void *arg) {
    control_server((const char *)arg);
    return NULL;
}


int main(int argc, char **argv) {
    char exe[PATH_MAX];
    uint32_t size = sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) != 0) return 1;

    char resolved[PATH_MAX];
    if (realpath(exe, resolved) == NULL) return 1;

    /* Beside the project, the root is four levels up. Installed in
     * /Applications it is not, so fall back to the known location — the copy
     * in /Applications is the one macOS can grant, and it still has to find
     * the code it runs. */
    char root[PATH_MAX];
    snprintf(root, sizeof(root), "%s", resolved);
    char *up = root;
    for (int i = 0; i < 4; i++) up = dirname(up);

    char probe[PATH_MAX];
    snprintf(probe, sizeof(probe), "%s/.venv/bin/python", up);
    if (access(probe, X_OK) != 0) {
        const char *home = getenv("HOME");
        if (home == NULL) return 1;
        snprintf(up, PATH_MAX, "%s/JARVIS", home);
    }
    if (chdir(up) != 0) return 1;
    char *rootdir = up;

    int checking = 0;
    for (int i = 1; i < argc; i++)
        if (strcmp(argv[i], "--check-control") == 0) checking = 1;

    /* Launched by LaunchServices there is no terminal, so say where output
     * went rather than dropping it. */
    mkdir("logs", 0755);
    const char *out = checking ? "logs/control-check.txt" : "logs/run.log";
    freopen(out, checking ? "w" : "a", stdout);
    freopen(out, "a", stderr);

    char python[PATH_MAX];
    snprintf(python, sizeof(python), "%s/.venv/bin/python", rootdir);

    char *args[64];
    int n = 0;
    args[n++] = python;
    args[n++] = "-u";
    args[n++] = "-m";
    args[n++] = "jarvis.main";
    for (int i = 1; i < argc && n < 60; i++) args[n++] = argv[i];
    args[n] = NULL;

    setenv("JARVIS_BUNDLED", "1", 1);

    /* Tell the child whether THIS process — the app — may post events, and
     * where to send them. */
    char sock[PATH_MAX];
    snprintf(sock, sizeof(sock), "%s/logs/control.sock", rootdir);
    int trusted = AXIsProcessTrusted() ? 1 : 0;
    setenv("JARVIS_CONTROL_SOCK", sock, 1);
    setenv("JARVIS_APP_TRUSTED", trusted ? "1" : "0", 1);
    fprintf(stderr, "  app bundle trusted for control: %s\n", trusted ? "yes" : "no");

    /* Deliberately fork rather than exec.
     *
     * execv would replace THIS process with Python, and TCC evaluates the code
     * signature a process currently has — so the thing asking for permission
     * would be "python3", not com.leitenberg.jarvis, and the grant given to the
     * app could never apply. Forking keeps this signed binary alive as the
     * parent, which makes it the responsible process for the child, and the
     * child inherits the app's grant.
     */
    pid_t child = fork();
    if (child < 0) {
        perror("could not fork");
        return 1;
    }
    if (child == 0) {
        execv(python, args);
        perror("could not start the assistant");
        _exit(1);
    }

    /* A diagnostic run has to finish before the caller reads its report. A
     * normal run must NOT keep this process resident: LaunchServices would
     * then treat a second click as "activate the running app" and nothing
     * would happen. Exiting lets every click start a launcher that either
     * starts the assistant or brings its interface up. */
    if (checking) {
        /* Serve while the check runs. Without this the check exercises the
         * direct-posting path, which is exactly the path that cannot work —
         * it reported "IGNORED" while the app itself was properly trusted. */
        pthread_t server;
        pthread_create(&server, NULL, serve_thread, (void *)sock);
        int status = 0;
        while (waitpid(child, &status, 0) < 0 && errno == EINTR) { }
        unlink(sock);
        return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
    }

    /* Take the assistant down with this process.
     *
     * Quitting the app kills this binary, not the Python it forked. Orphaned,
     * the child keeps running and keeps the camera — the window is gone, the
     * green light is on, and nothing on screen explains why. So every way
     * this process can be asked to stop is forwarded to the child, and the
     * child is given a moment to release the device before being insisted
     * upon. SIGKILL cannot be caught, here or anywhere. */
    g_child = child;
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = stop_child;
    sigemptyset(&sa.sa_mask);
    for (int i = 0; i < 4; i++) {
        int sigs[4] = { SIGTERM, SIGINT, SIGHUP, SIGQUIT };
        sigaction(sigs[i], &sa, NULL);
    }
    atexit(reap_child);

    /* Stay alive to serve control requests — but only for as long as there is
     * an assistant to serve. This binary outlives the interface on purpose,
     * because it is the signed process the Accessibility grant is attached to
     * and the relay has to come from it. Ignoring SIGCHLD meant it never
     * noticed the child had gone, so it sat there serving nothing: still in
     * the process list, still owning the port, indistinguishable from an app
     * that will not stop running in the background. It now exits with its
     * child. */
    struct sigaction child_sa;
    memset(&child_sa, 0, sizeof(child_sa));
    child_sa.sa_handler = child_gone;
    sigemptyset(&child_sa.sa_mask);
    child_sa.sa_flags = SA_NOCLDSTOP;
    sigaction(SIGCHLD, &child_sa, NULL);

    control_server(sock);
    reap_child();
    unlink(sock);
    return 0;
}
