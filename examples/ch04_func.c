/* ============================================================
   第4章 函数 —— 变量存储类型 / 参数按值传递 / 返回值
   ------------------------------------------------------------
   用 cdbg 调试本文件时可以观察到：
     · 全局变量 g_counter 落在 .data 段（已初始化）
     · 未初始化的全局变量 g_uninit 落在 .bss 段，值自动是 0
     · 函数内的普通局部变量 a、b 落在栈上，函数返回即回收
     · add() 的参数 x、y 是调用者变量的「副本」
     · static 变量 s_calls 只存在一份，跨多次调用保留
   ============================================================ */

#include <stdio.h>

int g_counter = 100;      /* 全局变量 → .data */
int g_uninit;             /* 未初始化全局变量 → .bss，值自动为 0 */
static int s_calls = 0;   /* 文件作用域静态变量 → .data，生命周期同程序 */

int add(int x, int y)     /* x、y 是参数：按值传递的副本 */
{
    int local = x + y;    /* 自动变量 → 栈 */
    s_calls++;
    return local;
}

void show(void)
{
    printf("counter=%d uninit=%d calls=%d\n", g_counter, g_uninit, s_calls);
}

int main(void)
{
    int a = 3;
    int b = 4;
    int sum;

    show();

    sum = add(a, b);       /* 传进去的是 a、b 的副本 */

    /* 此时在 cdbg 的内存视图里可以看到：a 和 b 的值没变，
       而 add 栈帧里的 x、y 是另外两个独立的变量。 */

    g_counter = g_counter + sum;
    show();

    printf("sum=%d\n", sum);
    return 0;
}
