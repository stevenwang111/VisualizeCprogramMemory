/* ch10_input.c —— 需要从键盘读输入的程序。
 *
 * 这个例子用来验证：
 *   - printf 的输出进到被调试程序自己的控制台窗口
 *   - scanf 能真的从键盘读到东西（不会跟调试器的命令管道抢 stdin）
 *
 * 调试时，程序的控制台是独立的一个窗口：
 *   - 在那个窗口里敲数字并回车，程序就能收到
 *   - 浏览器里的界面照常显示变量和内存
 */
#include <stdio.h>

int main(void)
{
    int a = 0;
    int b = 0;
    int sum = 0;

    printf("请输入两个整数（用空格隔开）：");
    fflush(stdout);

    if (scanf("%d %d", &a, &b) != 2) {
        printf("输入格式不对\n");
        return 1;
    }

    sum = a + b;

    printf("a = %d\n", a);
    printf("b = %d\n", b);
    printf("sum = %d\n", sum);

    return 0;
}
