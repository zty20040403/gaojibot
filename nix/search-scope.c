#include <err.h>
#include <linux/landlock.h>
#include <sys/syscall.h>
#include <unistd.h>

/* The unit shares the host network for HTTPS. Filesystem mounts do not hide
   host services listening on abstract Unix sockets. Require Landlock ABI 6. */
int main(int argc, char **argv) {
    if (argc < 2) errx(1, "missing search executable");
    struct landlock_ruleset_attr rules = {.scoped = LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET};
    int fd = syscall(SYS_landlock_create_ruleset, &rules, sizeof(rules), 0);
    if (fd < 0 || syscall(SYS_landlock_restrict_self, fd, 0) < 0) err(1, "search IPC scope");
    close(fd);
    execv(argv[1], argv + 1);
    err(1, "search exec");
}
