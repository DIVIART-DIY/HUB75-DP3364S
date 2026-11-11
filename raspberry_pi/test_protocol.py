"""No GPIO/DRM access. Checks the complete PWM template, not only RGB data."""
import unittest
import dp3364s_blue as panel
from patterns import picture
class FullCycleTests(unittest.TestCase):
    def test_all_64_groups_finish_before_selector_clear(self):
        lines=panel.build_words('black',full_pwm_cycle=True)
        self.assertEqual(len(lines),514);self.assertTrue(all(len(line)==644 for line in lines))
        previous=0;width=0;pulses=[];selector=0
        for line in lines:
            for word in line:
                if word & panel.A and not previous & panel.A:
                    selector=((selector<<1)|bool(word & panel.C))&((1<<40)-1)
                if word & panel.ROW:
                    self.assertFalse(word & panel.B);self.assertNotEqual(selector,0)
                    self.assertEqual(selector & (selector-1),0);width+=1
                elif width:pulses.append(width);width=0
                previous=word
        self.assertEqual(pulses,[12]+[4]*2559);self.assertEqual(selector,0)
    def test_black_image_is_exact_zero(self):self.assertEqual(picture('solid','black'),bytes(38400))
if __name__=='__main__':unittest.main()
