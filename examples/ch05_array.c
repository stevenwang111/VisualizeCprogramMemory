/* ============================================================
   第5章 数组 —— 连续布局 / 数组传参退化为指针
   ------------------------------------------------------------
   用 cdbg 可以观察到：
     · 数组 a 的 5 个元素在内存里紧挨着，地址相差正好 4 字节
     · 二维数组 b 的每一行也是连续的，且各行之间也紧挨着
     · sum_array(a) 的形参 int *p —— 数组传进函数后变成了指针，
       函数只知道首地址，不知道数组长度
   ============================================================ */

#include <stdio.h>

int g_arr[4] = {10, 20, 30, 40};      /* 全局数组 → .data */

int sum_array(int *p, int n)           /* 数组形参 → 指针 */
{
    int i;
    int sum = 0;
    for (i = 0; i < n; i++)
        sum += p[i];                     /* p[i] 等价于 *(p + i) */
    return sum;
}

void show_2d(int (*m)[3], int rows)     /* 指向数组的指针 */
{
    int i, j;
    for (i = 0; i < rows; i++) {
        for (j = 0; j < 3; j++)
            printf("%d ", m[i][j]);
        printf("\n");
    }
}

int main(void)
{
    int a[5] = {1, 2, 3, 4, 5};
    int b[2][3] = {{1, 2, 3}, {4, 5, 6}};
    int total;

    /* 在 cdbg 里看 a[0] 和 a[1] 的地址差：正好是 4（sizeof(int)） */
    printf("&a[0]=%p  &a[1]=%p\n", (void *)&a[0], (void *)&a[1]);

    total = sum_array(a, 5);
    printf("sum=%d\n", total);
    printf("global arr sum=%d\n", sum_array(g_arr, 4));

    show_2d(b, 2);
    return 0;
}
