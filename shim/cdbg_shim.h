/*
 * cdbg 插桩头 —— 堆跟踪
 * ============================================================
 * 这个头文件会被自动插入到用户源码的「最后一个 #include 之后」，
 * 作用是把 malloc / free 等堆函数重定向到我们的包装函数，
 * 从而精确记录每一次堆分配与释放。
 *
 * 为什么把标准库头放在宏定义之前：
 *   如果先定义 #define malloc(n) cdbg_malloc(n)，再 #include <stdlib.h>，
 *   那么 stdlib.h 里的 `void *malloc(size_t);` 声明会被替换成
 *   `void *cdbg_malloc(n);` —— 编译直接报错。
 *   先 include 再定义宏，就彻底避开了这个坑。
 *
 * 为什么记录 __FILE__ / __LINE__：
 *   教学上，「这块堆内存是在哪一行申请出来的」和「它有多大」同样重要。
 */

#ifndef CDBG_SHIM_H
#define CDBG_SHIM_H

#include <stdlib.h>
#include <stdio.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 包装函数原型。实现见 cdbg_shim.c */
void *cdbg_malloc_at(size_t size, const char *file, int line);
void  cdbg_free_at(void *ptr, const char *file, int line);
void *cdbg_calloc_at(size_t count, size_t size, const char *file, int line);
void *cdbg_realloc_at(void *ptr, size_t size, const char *file, int line);
char *cdbg_strdup_at(const char *s, const char *file, int line);

#ifdef __cplusplus
}
#endif

/* ---- 宏重定向 ---- */

#define malloc(n)          cdbg_malloc_at((n), __FILE__, __LINE__)
#define calloc(c, s)       cdbg_calloc_at((c), (s), __FILE__, __LINE__)
#define realloc(p, n)      cdbg_realloc_at((p), (n), __FILE__, __LINE__)
#define free(p)            cdbg_free_at((p), __FILE__, __LINE__)

#ifdef __cplusplus
#define strdup(s)          cdbg_strdup_at((s), __FILE__, __LINE__)
#endif

#endif /* CDBG_SHIM_H */
