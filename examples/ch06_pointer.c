/* ============================================================
   第6章 指针 —— 指针变量 / 多级指针 / 指针数组 / 指向函数的指针
   ------------------------------------------------------------
   用 cdbg 可以观察到：
     · 指针变量 p 自己占 8 字节，里面存的是「地址」而不是数据
     · p 的地址 和 p 指向的地址，是两个完全不同的数字
     · pp 是「指针的指针」，解引用两次才能拿到真正的值
     · names 是「指针数组」（每个元素都是 char*）
     · fp 是「指向函数的指针」，它存的是函数在代码段的地址
   ============================================================ */

#include <stdio.h>

int global_int = 42;

void hello(void)
{
    printf("hello from function pointer\n");
}

int main(void)
{
    int value = 7;
    int *p = &value;        /* p 里存着 value 的地址 */
    int **pp = &p;          /* pp 里存着 p 的地址 */
    int *null_ptr = 0;

    const char *names[3] = {"Tom", "Lily", "Bob"};   /* 指针数组 */
    void (*fp)(void) = hello;                        /* 指向函数的指针 */

    printf("value=%d\n", value);
    printf("&value=%p  p=%p  &p=%p\n",
           (void *)&value, (void *)p, (void *)&p);
    printf("**pp=%d\n", **pp);
    printf("null_ptr=%p\n", (void *)null_ptr);

    for (int i = 0; i < 3; i++)
        printf("names[%d]=%s\n", i, names[i]);

    fp();
    return 0;
}
