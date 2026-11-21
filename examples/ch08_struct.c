/* ============================================================
   第8章 结构与联合（选做）—— 内存布局与对齐
   ------------------------------------------------------------
   用 cdbg 可以观察到：
     · 结构体的成员在内存里是紧挨着排列的，但会为了对齐而留空隙
     · sizeof(struct) 不等于各成员大小之和 —— 多出来的是对齐用的填充
     · 联合体的所有成员共用同一块内存，大小等于最大的那个成员
     · 用字符数组逐字节看，能清楚看到中间的「填充字节」
   ============================================================ */

#include <stdio.h>
#include <string.h>

struct Student {
    int    id;        /* 4 字节 */
    char   name[8];   /* 8 字节 */
    double score;     /* 8 字节 */
    char   gender;    /* 1 字节 */
};                    /* 实际 sizeof 往往大于 4+8+8+1 */

union Value {
    int   i;
    float f;
    char  bytes[4];
};

struct Packed {
    char a;
    int  b;
    char c;
};

int main(void)
{
    struct Student s;
    struct Packed p;
    union Value u;
    int i;

    strcpy(s.name, "ZhangSan");
    s.id = 1001;
    s.score = 92.5;
    s.gender = 'M';

    printf("sizeof(struct Student) = %zu\n", sizeof(struct Student));
    printf("sizeof(union Value)    = %zu\n", sizeof(union Value));
    printf("sizeof(struct Packed)  = %zu\n", sizeof(struct Packed));

    printf("&s.id    = %p\n", (void *)&s.id);
    printf("&s.name  = %p\n", (void *)&s.name);
    printf("&s.score = %p\n", (void *)&s.score);
    printf("&s.gender= %p\n", (void *)&s.gender);

    /* 用字符数组看联合体里同一块内存的四种解释 */
    u.i = 0x41424344;
    printf("union as int   = %d\n", u.i);
    printf("union as bytes = %c%c%c%c\n",
           u.bytes[0], u.bytes[1], u.bytes[2], u.bytes[3]);

    /* 结构体填充字节：在 cdbg 里把 p 的字节逐个看，
       会看到 a 和 b 之间有一段空白 —— 那就是对齐填充 */
    p.a = 'x';
    p.b = 0x11223344;
    p.c = 'y';
    printf("packed: a=%c b=%x c=%c\n", p.a, p.b, p.c);

    for (i = 0; i < (int)sizeof(struct Packed); i++) {
        printf("byte %d = %02x\n", i, ((unsigned char *)&p)[i]);
    }

    return 0;
}
