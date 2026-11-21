/* ============================================================
   第9章 文件（选做）—— 缓冲区就是一块内存
   ------------------------------------------------------------
   用 cdbg 可以观察到：
     · fopen 返回的是 FILE*，它指向一个 FILE 结构体（里面还有缓冲区）
     · fgets 把内容读进你自己声明的 char 数组 —— 那块数组在栈上
     · fwrite/fread 操作的缓冲区同样是普通内存
     · fprintf 的 format 参数如果来自用户输入，就可能越界
   ============================================================ */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define LINES 5

int main(void)
{
    FILE *fp;
    char buf[64];
    char grid[LINES][32];
    int  i;

    /* 写一个临时文件 */
    fp = fopen("cdbg_demo.txt", "w");
    if (fp == NULL) {
        perror("fopen(w)");
        return 1;
    }
    for (i = 0; i < LINES; i++)
        fprintf(fp, "line %d: hello cdbg\n", i);
    fclose(fp);

    /* 读回来 */
    fp = fopen("cdbg_demo.txt", "r");
    if (fp == NULL) {
        perror("fopen(r)");
        return 1;
    }

    for (i = 0; i < LINES; i++) {
        if (fgets(grid[i], (int)sizeof(grid[i]), fp) == NULL)
            break;
        printf("read: %s", grid[i]);
    }

    /* 用 fgets 读进栈上的缓冲区 */
    rewind(fp);
    if (fgets(buf, (int)sizeof(buf), fp))
        printf("first line via buf: %s", buf);
    fclose(fp);

    remove("cdbg_demo.txt");
    return 0;
}
