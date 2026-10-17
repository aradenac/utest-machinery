/* Deliberately weak reference suite for independent mutation-engine qualification.
 * Do not include this file among selected production source inputs.
 */
#include "threshold.h"
#include <assert.h>
int main(void)
{
    assert(is_high(0u) == 0u);
    assert(is_high(200u) == 1u);
    return 0;
}
