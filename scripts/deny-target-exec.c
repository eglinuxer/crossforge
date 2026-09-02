#define _GNU_SOURCE

#include <dlfcn.h>
#include <elf.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <link.h>
#include <spawn.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

extern char **environ;

/*
 * This is a dynamic-libc/loader policy guard, not a sandbox. LD_PRELOAD
 * cannot mediate direct system calls or statically linked executables. The
 * build also runs without QEMU/HOSTRUNNER and is qualified independently.
 */

static int
path_is_below(const char *path, const char *root)
{
    size_t length = strlen(root);
    return strncmp(path, root, length) == 0
        && (path[length] == '\0' || path[length] == '/');
}

static void
log_denial(const char *kind, const char *resolved)
{
    char record[PATH_MAX + 32];
    const char *log_path = getenv("CROSSFORGE_DENY_EXEC_LOG");
    if (log_path == NULL) {
        return;
    }
    int length = snprintf(record, sizeof(record), "%s\t%s\n", kind, resolved);
    if (length < 0 || (size_t)length >= sizeof(record)) {
        return;
    }
    int log = open(log_path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600);
    if (log >= 0) {
        ssize_t written = write(log, record, (size_t)length);
        (void)written;
        close(log);
    }
}

static int
resolved_path_is_denied(const char *resolved, const char *kind)
{
    const char *roots = getenv("CROSSFORGE_DENY_EXEC_ROOTS");
    const char *machine_text = getenv("CROSSFORGE_DENY_EXEC_MACHINE");
    if (roots == NULL || machine_text == NULL) {
        return 0;
    }

    int below = 0;
    const char *start = roots;
    while (*start != '\0') {
        const char *end = strchr(start, ':');
        size_t length = end == NULL ? strlen(start) : (size_t)(end - start);
        char root[PATH_MAX];
        if (length > 0 && length < sizeof(root)) {
            memcpy(root, start, length);
            root[length] = '\0';
            if (path_is_below(resolved, root)) {
                below = 1;
                break;
            }
        }
        if (end == NULL) {
            break;
        }
        start = end + 1;
    }
    if (!below) {
        return 0;
    }

    char *machine_end = NULL;
    errno = 0;
    unsigned long machine = strtoul(machine_text, &machine_end, 10);
    if (errno != 0 || machine_end == machine_text || *machine_end != '\0'
        || machine > UINT16_MAX) {
        return 0;
    }

    Elf64_Ehdr header;
    size_t offset = 0;
    int descriptor = open(resolved, O_RDONLY | O_CLOEXEC);
    if (descriptor < 0) {
        return 0;
    }
    while (offset < sizeof(header)) {
        ssize_t count = read(
            descriptor,
            (unsigned char *)&header + offset,
            sizeof(header) - offset
        );
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            break;
        }
        offset += (size_t)count;
    }
    close(descriptor);
    if (offset != sizeof(header)
        || memcmp(header.e_ident, ELFMAG, SELFMAG) != 0
        || header.e_ident[EI_CLASS] != ELFCLASS64
        || header.e_ident[EI_DATA] != ELFDATA2LSB
        || header.e_machine != (Elf64_Half)machine) {
        return 0;
    }

    log_denial(kind, resolved);
    return 1;
}

static int
is_denied_target(const char *path, const char *kind)
{
    char resolved[PATH_MAX];
    if (path == NULL || realpath(path, resolved) == NULL) {
        return 0;
    }
    return resolved_path_is_denied(resolved, kind);
}

static int
is_executable_file(const char *path)
{
    struct stat metadata;
    return stat(path, &metadata) == 0
        && S_ISREG(metadata.st_mode)
        && access(path, X_OK) == 0;
}

static int
is_denied_search_target(const char *file, const char *kind)
{
    if (file == NULL || strchr(file, '/') != NULL) {
        return is_denied_target(file, kind);
    }

    char default_path[PATH_MAX];
    const char *search = getenv("PATH");
    if (search == NULL) {
        size_t length = confstr(_CS_PATH, default_path, sizeof(default_path));
        if (length == 0 || length > sizeof(default_path)) {
            search = "/bin:/usr/bin";
        } else {
            search = default_path;
        }
    }

    const char *start = search;
    while (1) {
        const char *end = strchr(start, ':');
        size_t length = end == NULL ? strlen(start) : (size_t)(end - start);
        char candidate[PATH_MAX];
        int written;
        if (length == 0) {
            written = snprintf(candidate, sizeof(candidate), "%s", file);
        } else {
            written = snprintf(
                candidate,
                sizeof(candidate),
                "%.*s/%s",
                (int)length,
                start,
                file
            );
        }
        if (written > 0 && (size_t)written < sizeof(candidate)
            && is_executable_file(candidate)) {
            return is_denied_target(candidate, kind);
        }
        if (end == NULL) {
            break;
        }
        start = end + 1;
    }
    return 0;
}

static int
is_denied_fd_target(int descriptor, const char *kind)
{
    char path[64];
    int written = snprintf(path, sizeof(path), "/proc/self/fd/%d", descriptor);
    if (written <= 0 || (size_t)written >= sizeof(path)) {
        return 0;
    }
    return is_denied_target(path, kind);
}

static int
is_denied_execveat_target(
    int directory,
    const char *path,
    int flags,
    const char *kind
)
{
    if (path == NULL) {
        return 0;
    }
    if (path[0] == '/') {
        return is_denied_target(path, kind);
    }
    if (path[0] == '\0' && (flags & AT_EMPTY_PATH) != 0) {
        return is_denied_fd_target(directory, kind);
    }
    if (directory == AT_FDCWD) {
        return is_denied_target(path, kind);
    }

    char combined[PATH_MAX];
    int written = snprintf(
        combined,
        sizeof(combined),
        "/proc/self/fd/%d/%s",
        directory,
        path
    );
    if (written <= 0 || (size_t)written >= sizeof(combined)) {
        return 0;
    }
    return is_denied_target(combined, kind);
}

static char **
collect_argv(const char *first, va_list arguments, char *const **environment)
{
    va_list counter;
    va_copy(counter, arguments);
    size_t count = 0;
    const char *value = first;
    while (value != NULL) {
        if (count == SIZE_MAX / sizeof(char *) - 1) {
            va_end(counter);
            errno = EOVERFLOW;
            return NULL;
        }
        count++;
        value = va_arg(counter, const char *);
    }
    if (environment != NULL) {
        *environment = va_arg(counter, char *const *);
    }
    va_end(counter);

    char **argv = calloc(count + 1, sizeof(char *));
    if (argv == NULL) {
        return NULL;
    }
    value = first;
    for (size_t index = 0; index < count; index++) {
        argv[index] = (char *)value;
        value = va_arg(arguments, const char *);
    }
    if (environment != NULL) {
        *environment = va_arg(arguments, char *const *);
    }
    return argv;
}

int
execve(const char *path, char *const argv[], char *const envp[])
{
    static int (*next_execve)(const char *, char *const[], char *const[]);
    if (is_denied_target(path, "execve")) {
        errno = EACCES;
        return -1;
    }
    if (next_execve == NULL) {
        next_execve = dlsym(RTLD_NEXT, "execve");
    }
    if (next_execve == NULL) {
        errno = ENOSYS;
        return -1;
    }
    return next_execve(path, argv, envp);
}

int
execv(const char *path, char *const argv[])
{
    static int (*next_execv)(const char *, char *const[]);
    if (is_denied_target(path, "execv")) {
        errno = EACCES;
        return -1;
    }
    if (next_execv == NULL) {
        next_execv = dlsym(RTLD_NEXT, "execv");
    }
    if (next_execv == NULL) {
        errno = ENOSYS;
        return -1;
    }
    return next_execv(path, argv);
}

int
execvp(const char *file, char *const argv[])
{
    static int (*next_execvp)(const char *, char *const[]);
    if (is_denied_search_target(file, "execvp")) {
        errno = EACCES;
        return -1;
    }
    if (next_execvp == NULL) {
        next_execvp = dlsym(RTLD_NEXT, "execvp");
    }
    if (next_execvp == NULL) {
        errno = ENOSYS;
        return -1;
    }
    return next_execvp(file, argv);
}

int
execvpe(const char *file, char *const argv[], char *const envp[])
{
    static int (*next_execvpe)(const char *, char *const[], char *const[]);
    if (is_denied_search_target(file, "execvpe")) {
        errno = EACCES;
        return -1;
    }
    if (next_execvpe == NULL) {
        next_execvpe = dlsym(RTLD_NEXT, "execvpe");
    }
    if (next_execvpe == NULL) {
        errno = ENOSYS;
        return -1;
    }
    return next_execvpe(file, argv, envp);
}

int
execl(const char *path, const char *first, ...)
{
    static int (*next_execv)(const char *, char *const[]);
    if (is_denied_target(path, "execl")) {
        errno = EACCES;
        return -1;
    }
    va_list arguments;
    va_start(arguments, first);
    char **argv = collect_argv(first, arguments, NULL);
    va_end(arguments);
    if (argv == NULL) {
        return -1;
    }
    if (next_execv == NULL) {
        next_execv = dlsym(RTLD_NEXT, "execv");
    }
    if (next_execv == NULL) {
        free(argv);
        errno = ENOSYS;
        return -1;
    }
    int result = next_execv(path, argv);
    int saved_errno = errno;
    free(argv);
    errno = saved_errno;
    return result;
}

int
execlp(const char *file, const char *first, ...)
{
    static int (*next_execvp)(const char *, char *const[]);
    if (is_denied_search_target(file, "execlp")) {
        errno = EACCES;
        return -1;
    }
    va_list arguments;
    va_start(arguments, first);
    char **argv = collect_argv(first, arguments, NULL);
    va_end(arguments);
    if (argv == NULL) {
        return -1;
    }
    if (next_execvp == NULL) {
        next_execvp = dlsym(RTLD_NEXT, "execvp");
    }
    if (next_execvp == NULL) {
        free(argv);
        errno = ENOSYS;
        return -1;
    }
    int result = next_execvp(file, argv);
    int saved_errno = errno;
    free(argv);
    errno = saved_errno;
    return result;
}

int
execle(const char *path, const char *first, ...)
{
    static int (*next_execve)(const char *, char *const[], char *const[]);
    if (is_denied_target(path, "execle")) {
        errno = EACCES;
        return -1;
    }
    va_list arguments;
    va_start(arguments, first);
    char *const *environment = NULL;
    char **argv = collect_argv(first, arguments, &environment);
    va_end(arguments);
    if (argv == NULL) {
        return -1;
    }
    if (next_execve == NULL) {
        next_execve = dlsym(RTLD_NEXT, "execve");
    }
    if (next_execve == NULL) {
        free(argv);
        errno = ENOSYS;
        return -1;
    }
    int result = next_execve(path, argv, (char *const *)environment);
    int saved_errno = errno;
    free(argv);
    errno = saved_errno;
    return result;
}

int
fexecve(int descriptor, char *const argv[], char *const envp[])
{
    static int (*next_fexecve)(int, char *const[], char *const[]);
    if (is_denied_fd_target(descriptor, "fexecve")) {
        errno = EACCES;
        return -1;
    }
    if (next_fexecve == NULL) {
        next_fexecve = dlsym(RTLD_NEXT, "fexecve");
    }
    if (next_fexecve == NULL) {
        errno = ENOSYS;
        return -1;
    }
    return next_fexecve(descriptor, argv, envp);
}

int
execveat(
    int directory,
    const char *path,
    char *const argv[],
    char *const envp[],
    int flags
)
{
    static int (*next_execveat)(
        int,
        const char *,
        char *const[],
        char *const[],
        int
    );
    if (is_denied_execveat_target(directory, path, flags, "execveat")) {
        errno = EACCES;
        return -1;
    }
    if (next_execveat == NULL) {
        next_execveat = dlsym(RTLD_NEXT, "execveat");
    }
    if (next_execveat == NULL) {
        errno = ENOSYS;
        return -1;
    }
    return next_execveat(directory, path, argv, envp, flags);
}

int
posix_spawn(
    pid_t *pid,
    const char *path,
    const posix_spawn_file_actions_t *actions,
    const posix_spawnattr_t *attributes,
    char *const argv[],
    char *const envp[]
)
{
    static int (*next_posix_spawn)(
        pid_t *,
        const char *,
        const posix_spawn_file_actions_t *,
        const posix_spawnattr_t *,
        char *const[],
        char *const[]
    );
    if (is_denied_target(path, "posix_spawn")) {
        return EACCES;
    }
    if (next_posix_spawn == NULL) {
        next_posix_spawn = dlsym(RTLD_NEXT, "posix_spawn");
    }
    if (next_posix_spawn == NULL) {
        return ENOSYS;
    }
    return next_posix_spawn(pid, path, actions, attributes, argv, envp);
}

int
posix_spawnp(
    pid_t *pid,
    const char *file,
    const posix_spawn_file_actions_t *actions,
    const posix_spawnattr_t *attributes,
    char *const argv[],
    char *const envp[]
)
{
    static int (*next_posix_spawnp)(
        pid_t *,
        const char *,
        const posix_spawn_file_actions_t *,
        const posix_spawnattr_t *,
        char *const[],
        char *const[]
    );
    if (is_denied_search_target(file, "posix_spawnp")) {
        return EACCES;
    }
    if (next_posix_spawnp == NULL) {
        next_posix_spawnp = dlsym(RTLD_NEXT, "posix_spawnp");
    }
    if (next_posix_spawnp == NULL) {
        return ENOSYS;
    }
    return next_posix_spawnp(pid, file, actions, attributes, argv, envp);
}

void *
dlopen(const char *path, int flags)
{
    static void *(*next_dlopen)(const char *, int);
    if (is_denied_target(path, "dlopen")) {
        errno = EACCES;
        return NULL;
    }
    if (next_dlopen == NULL) {
        next_dlopen = dlsym(RTLD_NEXT, "dlopen");
    }
    if (next_dlopen == NULL) {
        errno = ENOSYS;
        return NULL;
    }
    return next_dlopen(path, flags);
}

void *
dlmopen(Lmid_t namespace_id, const char *path, int flags)
{
    static void *(*next_dlmopen)(Lmid_t, const char *, int);
    if (is_denied_target(path, "dlmopen")) {
        errno = EACCES;
        return NULL;
    }
    if (next_dlmopen == NULL) {
        next_dlmopen = dlsym(RTLD_NEXT, "dlmopen");
    }
    if (next_dlmopen == NULL) {
        errno = ENOSYS;
        return NULL;
    }
    return next_dlmopen(namespace_id, path, flags);
}
