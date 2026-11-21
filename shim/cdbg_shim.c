/*
 * cdbg 插桩实现 —— 堆跟踪
 * ============================================================
 * 关键约束：**这个文件不能 include cdbg_shim.h**。
 * 一旦 include 了，宏重定向会作用到这里，cdbg_malloc 内部再调用
 * malloc 就会无限递归。
 *
 * 记录格式（每行一条，写到环境变量 CDBG_HEAP_LOG 指定的文件）：
 *   M <地址> <大小> <文件> <行号>      分配
 *   F <地址> <文件> <行号>              释放
 *   R <旧地址> <新地址> <大小> <行号>   重分配
 */

#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <stdarg.h>

#ifdef _WIN32
#include <windows.h>
static CRITICAL_SECTION g_lock;
static LONG g_lock_ready = 0;
#endif

static FILE *g_log = NULL;
static int g_log_tried = 0;

/*
 * 账本路径的优先级：环境变量 > 编译期常量。
 *
 * 为什么要编译期常量兜底：GDB 的 `set environment` 在 Windows 原生
 * 调试下不一定生效（实测被调试程序读不到 CDBG_HEAP_LOG）。
 * 所以插桩模块在编译前会把实际路径直接写进这个宏，保证一定能记到账。
 */
#ifndef CDBG_HEAP_LOG_PATH
#define CDBG_HEAP_LOG_PATH NULL
#endif

static const char *cdbg_log_path(void)
{
    const char *env = getenv("CDBG_HEAP_LOG");
    if (env && *env)
        return env;
    return CDBG_HEAP_LOG_PATH;
}

static void cdbg_lock_init(void)
{
#ifdef _WIN32
    if (!InterlockedCompareExchange(&g_lock_ready, 1, 0)) {
        InitializeCriticalSection(&g_lock);
    }
#endif
}

static void cdbg_lock(void)
{
#ifdef _WIN32
    cdbg_lock_init();
    EnterCriticalSection(&g_lock);
#endif
}

static void cdbg_unlock(void)
{
#ifdef _WIN32
    LeaveCriticalSection(&g_lock);
#endif
}

/* 惰性打开日志文件；写失败也绝不影响被调试程序的正常行为 */
static FILE *cdbg_log_file(void)
{
    if (g_log_tried)
        return g_log;
    g_log_tried = 1;

    const char *path = cdbg_log_path();
    if (!path || !*path)
        return NULL;

    /* 追加模式：Windows 下对同一文件的追加写是原子的，
       即使程序开了多线程也不会串行。 */
    g_log = fopen(path, "a");
    if (g_log)
        setvbuf(g_log, NULL, _IOLBF, 0);
    return g_log;
}

static void cdbg_emit(const char *fmt, ...)
{
    FILE *f = cdbg_log_file();
    if (!f)
        return;

    cdbg_lock();
    va_list ap;
    va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fflush(f);
    cdbg_unlock();
}

/* 只保留文件名部分，日志更好读 */
static const char *cdbg_basename(const char *path)
{
    const char *slash = path;
    const char *p;
    for (p = path; *p; p++) {
        if (*p == '/' || *p == '\\')
            slash = p + 1;
    }
    return slash;
}

void *cdbg_malloc_at(size_t size, const char *file, int line)
{
    void *p = malloc(size);
    cdbg_emit("M %p %zu %s %d\n", p, size, cdbg_basename(file), line);
    return p;
}

void cdbg_free_at(void *ptr, const char *file, int line)
{
    cdbg_emit("F %p %s %d\n", ptr, cdbg_basename(file), line);
    free(ptr);
}

void *cdbg_calloc_at(size_t count, size_t size, const char *file, int line)
{
    void *p = calloc(count, size);
    cdbg_emit("M %p %zu %s %d\n", p, count * size, cdbg_basename(file), line);
    return p;
}

void *cdbg_realloc_at(void *ptr, size_t size, const char *file, int line)
{
    void *p = realloc(ptr, size);
    cdbg_emit("R %p %p %zu %s %d\n", ptr, p, size, cdbg_basename(file), line);
    return p;
}

char *cdbg_strdup_at(const char *s, const char *file, int line)
{
    char *p = strdup(s);
    cdbg_emit("M %p %zu %s %d\n", p, s ? strlen(s) + 1 : 0, cdbg_basename(file), line);
    return p;
}
