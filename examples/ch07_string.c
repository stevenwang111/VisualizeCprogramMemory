/* ============================================================
   第7章 字符串 —— 字符串常量 / 字符数组 vs 字符指针
   ------------------------------------------------------------
   用 cdbg 可以观察到：
     · s1（字符数组）：字符就存在它自己那块栈空间里，可以改
     · s2（字符指针）：它指向的是 .rdata 里的只读常量，改了会崩
     · "hello" 这个字面量在编译期就写进了只读数据区
     · 两个指针指向同一个字面量时，地址完全相同（编译器会合并常量）
   ============================================================ */

#include <stdio.h>
#include <string.h>

const char *g_msg = "global string";   /* 指针 → .rdata 的字符串常量 */

int main(void)
{
    char s1[20] = "hello";       /* 字符数组：内容在栈上，可修改 */
    const char *s2 = "hello";    /* 字符指针：指向只读常量 */
    char buf[64];

    strcpy(buf, s1);
    strcat(buf, " world");
    printf("s1=%s len=%zu\n", s1, strlen(s1));
    printf("s2=%s\n", s2);
    printf("buf=%s\n", buf);
    printf("g_msg=%s\n", g_msg);

    /* s1[0] = 'H';   合法：字符数组可以改 */
    s1[0] = 'H';
    printf("modified s1=%s\n", s1);
    /* s2[0] = 'H';   非法：只读区，写了会崩 */

    return 0;
}
