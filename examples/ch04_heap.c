/* ============================================================
   堆 —— malloc / free 的内存生命周期
   ------------------------------------------------------------
   用 cdbg 调试本文件时可以观察到：
     · 每一块堆内存的起始地址、字节大小、是在哪一行申请出来的
     · 指针变量里存的地址，正好等于堆块的起始地址
     · free 之后，这块内存会从「活跃堆块」列表里消失
     · 忘记 free 就是内存泄漏 —— 观察程序结束时还剩多少活跃块

   注意：本文件会被自动插入一行 #include "cdbg_shim.h"，
   把 malloc / free 重定向到跟踪版本。用户代码本身一行没改。
   ============================================================ */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int g_counter = 0;

/* 在堆上申请一块内存，并填上一些内容 */
int *make_buffer(int n)
{
    int *buf = (int *)malloc(sizeof(int) * n);   /* 堆块起点 */
    int i;
    for (i = 0; i < n; i++)
        buf[i] = i * i;
    g_counter++;
    return buf;
}

char *make_greeting(const char *name)
{
    size_t len = strlen(name) + 8;
    char *s = (char *)malloc(len);               /* 另一块堆内存 */
    strcpy(s, "hello, ");
    strcat(s, name);
    return s;
}

int main(void)
{
    int *a;
    int *b;
    char *msg;

    a = make_buffer(10);
    b = make_buffer(20);
    msg = make_greeting("C语言");

    printf("a[0]=%d a[9]=%d\n", a[0], a[9]);
    printf("b[19]=%d\n", b[19]);
    printf("%s\n", msg);

    /* 把 b 释放掉：观察活跃堆块列表的变化 */
    free(b);

    /* 这里故意不 free(a) 和 msg —— 演示内存泄漏。
       程序结束时，活跃堆块列表里还剩下它们。 */

    return 0;
}
