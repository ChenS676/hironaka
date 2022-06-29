import unittest

from treelib import Tree

from hironaka.host import Zeillinger
from hironaka.abs import Points
from hironaka.abs.src import make_nested_list
from hironaka.util import search_depth, search_tree


class TestSearch(unittest.TestCase):
    def test_search_depth(self):
        points = Points(make_nested_list(
            [[(7, 5, 3, 8), (8, 1, 8, 18), (8, 3, 17, 8),
              (11, 11, 1, 19), (11, 12, 18, 6), (16, 11, 5, 6)]]
        ))
        pt_lst = [(7, 5, 3, 8), (8, 1, 8, 18), (8, 3, 17, 8),
                  (11, 11, 1, 19), (11, 12, 18, 6), (16, 11, 5, 6)]

        r = search_depth(points, Zeillinger())
        # r = searchDepth_2(pt_lst, Zeillinger())
        print(r)
        assert r == 5552

    def test_search_depth_small(self):
        points = Points(make_nested_list([(0, 1, 0, 1), (0, 2, 0, 0), (1, 0, 0, 1),
                                          (1, 0, 1, 0), (1, 1, 0, 0), (2, 0, 0, 0)]))
        r = search_depth(points, Zeillinger())
        print(r)
        assert r == 6

    def test_search(self):
        host = Zeillinger()
        points = Points(make_nested_list([(0, 0, 4), (5, 0, 1), (1, 5, 1), (0, 25, 0)]))

        tree = Tree()
        tree.create_node(0, 0, data=points)

        search_tree(points, tree, 0, host)
        tree.show(data_property="points", idhidden=False)
